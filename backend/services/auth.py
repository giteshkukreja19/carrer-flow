"""Single-user authentication with opaque, database-backed browser sessions."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Request
from lib import db

SESSION_COOKIE_NAME = "career_flow_session"
DEFAULT_SESSION_HOURS = 12
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


class AuthStoreUnavailable(RuntimeError):
    pass


MAX_LOGIN_FAILURES = 5
LOGIN_LOCK_MINUTES = 15


def credentials_configured() -> bool:
    return bool(os.environ.get("AUTH_USERNAME", "").strip() and os.environ.get("AUTH_PASSWORD", ""))


def credentials_match(username: str, password: str) -> bool:
    expected_username = os.environ.get("AUTH_USERNAME", "")
    expected_password = os.environ.get("AUTH_PASSWORD", "")
    username_ok = hmac.compare_digest(username.encode("utf-8"), expected_username.encode("utf-8"))
    password_ok = hmac.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
    return bool(expected_username and expected_password and username_ok and password_ok)


def session_ttl() -> timedelta:
    try:
        hours = int(os.environ.get("AUTH_SESSION_TTL_HOURS", str(DEFAULT_SESSION_HOURS)))
    except ValueError:
        hours = DEFAULT_SESSION_HOURS
    return timedelta(hours=max(1, min(hours, 168)))


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _client_digest(client_host: str) -> str:
    key = os.environ.get("AUTH_PASSWORD", "").encode("utf-8")
    return hmac.new(key, client_host.encode("utf-8"), hashlib.sha256).hexdigest()


async def _execute(query: str, *args: Any) -> None:
    if not await db.execute(query, *args):
        raise AuthStoreUnavailable("Authentication storage is unavailable.")


async def login_allowed(client_host: str) -> bool:
    if db.pool is None:
        raise AuthStoreUnavailable("Authentication storage is unavailable.")
    digest = _client_digest(client_host)
    try:
        row = await db.fetch_one(
            "SELECT failed_attempts, locked_until FROM auth_login_attempts WHERE client_hash=$1",
            digest,
        )
        if row and row.get("locked_until") and row["locked_until"] > datetime.now(timezone.utc):
            return False
        if row and row.get("locked_until"):
            await _execute("DELETE FROM auth_login_attempts WHERE client_hash=$1", digest)
        return True
    except Exception as exc:
        raise AuthStoreUnavailable("Authentication storage is unavailable.") from exc


async def record_failed_login(client_host: str) -> None:
    if db.pool is None:
        raise AuthStoreUnavailable("Authentication storage is unavailable.")
    digest = _client_digest(client_host)
    try:
        row = await db.fetch_one(
            "SELECT failed_attempts, locked_until FROM auth_login_attempts WHERE client_hash=$1",
            digest,
        )
        failures = int((row or {}).get("failed_attempts", 0)) + 1
        locked_until = (
            datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCK_MINUTES)
            if failures >= MAX_LOGIN_FAILURES else None
        )
        await _execute(
            """INSERT INTO auth_login_attempts (client_hash, failed_attempts, locked_until, updated_at)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (client_hash) DO UPDATE SET failed_attempts=EXCLUDED.failed_attempts,
                 locked_until=EXCLUDED.locked_until, updated_at=EXCLUDED.updated_at""",
            digest, failures, locked_until, datetime.now(timezone.utc),
        )
    except Exception as exc:
        raise AuthStoreUnavailable("Authentication storage is unavailable.") from exc


async def clear_login_failures(client_host: str) -> None:
    if db.pool is None:
        raise AuthStoreUnavailable("Authentication storage is unavailable.")
    try:
        await _execute("DELETE FROM auth_login_attempts WHERE client_hash=$1", _client_digest(client_host))
    except Exception as exc:
        raise AuthStoreUnavailable("Authentication storage is unavailable.") from exc


async def create_session(username: str) -> tuple[str, datetime]:
    if db.pool is None:
        raise AuthStoreUnavailable("Authentication storage is unavailable.")
    now = datetime.now(timezone.utc)
    expires_at = now + session_ttl()
    session_id = secrets.token_urlsafe(32)
    try:
        # Keep one active browser session for this single-user application.
        await _execute("DELETE FROM auth_sessions WHERE username=$1", username)
        await _execute(
            "INSERT INTO auth_sessions (session_hash, username, created_at, expires_at) VALUES ($1,$2,$3,$4)",
            _session_digest(session_id), username, now, expires_at,
        )
    except Exception as exc:
        raise AuthStoreUnavailable("Authentication storage is unavailable.") from exc
    return session_id, expires_at


async def find_session(session_id: str) -> dict[str, Any] | None:
    if not credentials_configured():
        return None
    if not _SESSION_ID.fullmatch(session_id):
        return None
    if db.pool is None:
        raise AuthStoreUnavailable("Authentication storage is unavailable.")
    digest = _session_digest(session_id)
    try:
        row = await db.fetch_one(
            "SELECT username, expires_at FROM auth_sessions WHERE session_hash=$1 AND expires_at > NOW()",
            digest,
        )
        if row and not hmac.compare_digest(
            str(row.get("username", "")).encode("utf-8"),
            os.environ.get("AUTH_USERNAME", "").encode("utf-8"),
        ):
            row = None
        if row is None:
            await _execute("DELETE FROM auth_sessions WHERE session_hash=$1", digest)
        return row
    except Exception as exc:
        raise AuthStoreUnavailable("Authentication storage is unavailable.") from exc


async def revoke_session(session_id: str) -> None:
    if db.pool is None:
        raise AuthStoreUnavailable("Authentication storage is unavailable.")
    try:
        await _execute("DELETE FROM auth_sessions WHERE session_hash=$1", _session_digest(session_id))
    except Exception as exc:
        raise AuthStoreUnavailable("Authentication storage is unavailable.") from exc


def cookie_secure(request: Request) -> bool:
    setting = os.environ.get("AUTH_COOKIE_SECURE", "auto").strip().lower()
    if setting in {"1", "true", "yes", "on"}:
        return True
    if setting in {"0", "false", "no", "off"}:
        return False
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def cookie_samesite() -> str:
    value = os.environ.get("AUTH_COOKIE_SAMESITE", "lax").strip().lower()
    return value if value in {"lax", "strict", "none"} else "lax"


def allowed_origins() -> set[str]:
    values = os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return {item.strip().rstrip("/") for item in values.split(",") if item.strip() and item.strip() != "*"}
