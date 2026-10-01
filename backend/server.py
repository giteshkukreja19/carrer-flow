import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from lib import db  # noqa: E402
from routers.auth import router as auth_router  # noqa: E402
from routers.placement import router as placement_router  # noqa: E402
from routers.gmail import router as gmail_router  # noqa: E402
from services.notifications import whatsapp_requested  # noqa: E402
from services.scheduler import gmail_poll_loop, whatsapp_notification_loop  # noqa: E402
from services.auth import (  # noqa: E402
    SESSION_COOKIE_NAME,
    AuthStoreUnavailable,
    allowed_origins,
    cookie_samesite,
    cookie_secure,
    find_session,
)

logger = logging.getLogger(__name__)


LOCAL_CORS_ORIGINS = ("http://localhost:5173", "http://127.0.0.1:5173")


def configured_cors_origins(value: str | None = None) -> list[str]:
    """Return explicit allowed origins; never default to a credentialed wildcard."""

    raw = os.environ.get("CORS_ORIGINS", "") if value is None else value
    origins = [origin.strip().rstrip("/") for origin in raw.split(",") if origin.strip()]
    if not origins:
        return list(LOCAL_CORS_ORIGINS)
    if "*" in origins:
        raise ValueError("CORS_ORIGINS must list explicit origins; wildcard CORS is disabled.")
    return origins


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect_db()
    app.state.gmail_poll_task = asyncio.create_task(gmail_poll_loop())
    app.state.whatsapp_notification_task = (
        asyncio.create_task(whatsapp_notification_loop()) if whatsapp_requested() else None
    )
    yield
    app.state.gmail_poll_task.cancel()
    tasks = [app.state.gmail_poll_task]
    if app.state.whatsapp_notification_task:
        app.state.whatsapp_notification_task.cancel()
        tasks.append(app.state.whatsapp_notification_task)
    await asyncio.gather(*tasks, return_exceptions=True)
    await db.close_db()


app = FastAPI(title="Career Flow", lifespan=lifespan)
api_router = APIRouter(prefix="/api")
api_router.include_router(auth_router)
api_router.include_router(placement_router)
api_router.include_router(gmail_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=configured_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.middleware("http")
async def require_api_session(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()
    is_api = path == "/api" or path.startswith("/api/")
    if not is_api or method == "OPTIONS":
        return await call_next(request)

    origin = request.headers.get("origin")
    if method not in {"GET", "HEAD", "OPTIONS"} and origin:
        if origin.rstrip("/") not in allowed_origins():
            return JSONResponse({"detail": "Request origin is not allowed."}, status_code=403)

    if (path, method) == ("/api/health", "GET") or (path, method) == ("/api/auth/login", "POST"):
        return await call_next(request)

    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return JSONResponse({"detail": "Authentication required."}, status_code=401)
    try:
        identity = await find_session(session_id)
    except AuthStoreUnavailable:
        return JSONResponse({"detail": "Authentication storage is unavailable."}, status_code=503)
    if identity is None:
        response = JSONResponse({"detail": "Authentication required."}, status_code=401)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/api",
            httponly=True,
            secure=cookie_secure(request) or cookie_samesite() == "none",
            samesite=cookie_samesite(),
        )
        return response
    request.state.identity = identity
    return await call_next(request)
