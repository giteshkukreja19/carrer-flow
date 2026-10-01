import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from services.auth import (
    SESSION_COOKIE_NAME,
    AuthStoreUnavailable,
    cookie_samesite,
    cookie_secure,
    clear_login_failures,
    create_session,
    credentials_configured,
    credentials_match,
    login_allowed,
    record_failed_login,
    revoke_session,
    session_ttl,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    if not credentials_configured():
        raise HTTPException(status_code=503, detail="Single-user login is not configured on the server.")
    try:
        raw_body = await request.body()
        if len(raw_body) > 4096:
            raise HTTPException(status_code=400, detail="Login request is too large.")
        body = json.loads(raw_body)
    except HTTPException:
        raise
    except Exception:
        body = None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="A username and password are required.")
    username = body.get("username") if isinstance(body.get("username"), str) else ""
    password = body.get("password") if isinstance(body.get("password"), str) else ""
    client_host = request.client.host if request.client else "unknown"
    try:
        if not await login_allowed(client_host):
            raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    except AuthStoreUnavailable:
        raise HTTPException(status_code=503, detail="Authentication storage is unavailable.") from None
    if not credentials_match(username, password):
        try:
            await record_failed_login(client_host)
        except AuthStoreUnavailable:
            raise HTTPException(status_code=503, detail="Authentication storage is unavailable.") from None
        raise HTTPException(status_code=401, detail="Invalid username or password.")
    try:
        await clear_login_failures(client_host)
        session_id, expires_at = await create_session(username)
    except AuthStoreUnavailable:
        raise HTTPException(status_code=503, detail="Authentication storage is unavailable.") from None
    ttl = int(session_ttl().total_seconds())
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=ttl,
        expires=ttl,
        httponly=True,
        secure=cookie_secure(request) or cookie_samesite() == "none",
        samesite=cookie_samesite(),
        path="/api",
    )
    return {"authenticated": True, "username": username, "expires_at": expires_at}


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    identity = getattr(request.state, "identity", None)
    if not identity:
        raise HTTPException(status_code=401, detail="Authentication required.")
    return {"authenticated": True, "username": identity["username"]}


@router.post("/logout", status_code=200)
async def logout(request: Request, response: Response) -> dict[str, bool]:
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        try:
            await revoke_session(session_id)
        except AuthStoreUnavailable:
            raise HTTPException(status_code=503, detail="Authentication storage is unavailable.") from None
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/api",
        httponly=True,
        secure=cookie_secure(request) or cookie_samesite() == "none",
        samesite=cookie_samesite(),
    )
    return {"authenticated": False}
