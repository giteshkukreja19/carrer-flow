"""Offline ASGI tests for the single-user cookie authentication boundary."""

import asyncio
import json
from http.cookies import SimpleCookie

import pytest

from lib import db
from routers.auth import SESSION_COOKIE_NAME
from server import app


def async_test(function):
    from functools import wraps

    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


async def asgi_request(path, method="GET", *, body=None, cookie=None, origin="http://localhost:5173"):
    raw_body = b"" if body is None else json.dumps(body).encode()
    headers = [(b"host", b"testserver")]
    if raw_body:
        headers.append((b"content-type", b"application/json"))
    if origin:
        headers.append((b"origin", origin.encode()))
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "query_string": b"", "root_path": "",
        "headers": headers, "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
        "state": {},
    }
    receive_queue = [{"type": "http.request", "body": raw_body, "more_body": False}]
    sent = []

    async def receive():
        if receive_queue:
            return receive_queue.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(item for item in sent if item["type"] == "http.response.start")
    response_body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), response_body


def install_store(monkeypatch, *, lookup=None):
    monkeypatch.setattr(db, "pool", object())
    calls = []

    async def execute(query, *args):
        calls.append((query, args))
        return True

    async def fetch_one(query, *args):
        calls.append((query, args))
        return lookup(*args) if lookup else None

    monkeypatch.setattr(db, "execute", execute)
    monkeypatch.setattr(db, "fetch_one", fetch_one)
    return calls


def cookie_from(headers):
    response_headers = [(key.decode(), value.decode()) for key, value in headers.items()]
    set_cookie = next(value for key, value in response_headers if key.lower() == "set-cookie")
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    return parsed[SESSION_COOKIE_NAME].value, set_cookie


@async_test
async def test_valid_login_sets_http_only_secure_cookie(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "single-user")
    monkeypatch.setenv("AUTH_PASSWORD", "test-password-from-mock")
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "true")
    calls = install_store(monkeypatch)

    status, headers, body = await asgi_request("/api/auth/login", "POST", body={"username": "single-user", "password": "test-password-from-mock"})
    cookie, serialized = cookie_from(headers)
    assert status == 200
    assert cookie
    assert "httponly" in serialized.lower()
    assert "secure" in serialized.lower()
    assert "samesite=lax" in serialized.lower()
    assert b"test-password-from-mock" not in body
    inserted = next(args for query, args in calls if "INSERT INTO auth_sessions" in query)
    assert len(inserted[0]) == 64
    assert inserted[0] != cookie


@async_test
async def test_invalid_login_does_not_create_session(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "single-user")
    monkeypatch.setenv("AUTH_PASSWORD", "correct")
    calls = install_store(monkeypatch)

    status, _, body = await asgi_request("/api/auth/login", "POST", body={"username": "single-user", "password": "wrong"})
    assert status == 401
    assert json.loads(body)["detail"] == "Invalid username or password."
    assert not any("INSERT INTO auth_sessions" in query for query, _ in calls)


@async_test
async def test_session_me_and_logout_revoke_session(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "single-user")
    monkeypatch.setenv("AUTH_PASSWORD", "correct")
    calls = install_store(monkeypatch, lookup=lambda *_: {"username": "single-user"})
    status, headers, _ = await asgi_request("/api/auth/login", "POST", body={"username": "single-user", "password": "correct"})
    session_cookie, _ = cookie_from(headers)
    assert status == 200

    status, _, body = await asgi_request("/api/auth/me", cookie=f"{SESSION_COOKIE_NAME}={session_cookie}")
    assert status == 200
    assert json.loads(body) == {"authenticated": True, "username": "single-user"}

    status, headers, body = await asgi_request("/api/auth/logout", "POST", cookie=f"{SESSION_COOKIE_NAME}={session_cookie}")
    assert status == 200
    assert json.loads(body) == {"authenticated": False}
    assert any("DELETE FROM auth_sessions WHERE session_hash" in query for query, _ in calls)
    assert any(key.lower() == b"set-cookie" and b"Max-Age=0" in value for key, value in headers.items())


@async_test
async def test_valid_session_can_read_protected_settings(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "single-user")
    monkeypatch.setenv("AUTH_PASSWORD", "correct")
    monkeypatch.setattr(db, "pool", object())

    async def fetch_one(query, *_):
        if "auth_sessions" in query:
            return {"username": "single-user"}
        return None

    monkeypatch.setattr(db, "fetch_one", fetch_one)
    status, _, body = await asgi_request("/api/settings", cookie=f"{SESSION_COOKIE_NAME}={'C' * 43}")
    assert status == 200
    settings = json.loads(body)
    assert settings["auto_apply_enabled"] is False
    assert settings["attendance_automation_enabled"] is False


@async_test
async def test_expired_session_is_rejected_and_cookie_cleared(monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", "single-user")
    monkeypatch.setenv("AUTH_PASSWORD", "correct")
    install_store(monkeypatch, lookup=lambda *_: None)
    status, headers, _ = await asgi_request("/api/auth/me", cookie=f"{SESSION_COOKIE_NAME}={'B' * 43}")
    assert status == 401
    assert any(key.lower() == b"set-cookie" and b"Max-Age=0" in value for key, value in headers.items())


@async_test
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/dashboard"),
    ("GET", "/api/settings"),
    ("GET", "/api/answer-profile"),
    ("PATCH", "/api/alerts/not-found/triage"),
    ("GET", "/api/gmail/status"),
    ("POST", "/api/gmail/poll"),
    ("POST", "/api/scan"),
])
async def test_protected_api_paths_require_login(method, path):
    status, _, _ = await asgi_request(path, method, body={} if method in {"POST", "PATCH"} else None, origin=None)
    assert status == 401


@async_test
async def test_health_remains_public():
    status, _, body = await asgi_request("/api/health", origin=None)
    assert status == 200
    assert json.loads(body)["status"] == "ok"


@async_test
async def test_disallowed_mutation_origin_is_rejected():
    status, _, _ = await asgi_request("/api/auth/login", "POST", body={"username": "x", "password": "x"}, origin="https://attacker.example")
    assert status == 403


@async_test
async def test_login_throttles_repeated_failures(monkeypatch):
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv("AUTH_USERNAME", "single-user")
    monkeypatch.setenv("AUTH_PASSWORD", "correct")
    monkeypatch.setattr(db, "pool", object())
    state = {"failed_attempts": 0, "locked_until": None}

    async def fetch_one(query, *_):
        return state.copy() if "auth_login_attempts" in query else None

    async def execute(query, *args):
        if "INSERT INTO auth_login_attempts" in query:
            state["failed_attempts"] = args[1]
            state["locked_until"] = args[2]
        return True

    monkeypatch.setattr(db, "fetch_one", fetch_one)
    monkeypatch.setattr(db, "execute", execute)
    for _ in range(5):
        status, _, _ = await asgi_request("/api/auth/login", "POST", body={"username": "single-user", "password": "wrong"})
        assert status == 401
    status, _, body = await asgi_request("/api/auth/login", "POST", body={"username": "single-user", "password": "correct"})
    assert status == 429
    assert "Too many login attempts" in json.loads(body)["detail"]
    assert state["locked_until"] > datetime.now(timezone.utc) - timedelta(seconds=1)
