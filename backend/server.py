import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI
from starlette.middleware.cors import CORSMiddleware

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

from lib import db  # noqa: E402
from routers.placement import router as placement_router  # noqa: E402
from routers.gmail import router as gmail_router  # noqa: E402
from services.notifications import whatsapp_requested  # noqa: E402
from services.scheduler import gmail_poll_loop, whatsapp_notification_loop  # noqa: E402

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


app = FastAPI(title="Placement Pilot", lifespan=lifespan)
api_router = APIRouter(prefix="/api")
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
