import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from lib import db
from models.placement import GmailIntegrationStatus
from services.gmail import (
    GmailAuthorizationError,
    GmailConfigurationError,
    GmailServiceError,
    GmailStorageError,
    complete_authorization,
    create_authorization,
    integration_status,
)
from services.ingestion import poll_gmail_once

router = APIRouter(prefix="/gmail", tags=["gmail"])


@router.get("/status", response_model=GmailIntegrationStatus)
async def gmail_status() -> GmailIntegrationStatus:
    settings = await db.fetch_one("SELECT haveloc_notification_sender, gmail_polling_enabled FROM scheduler_settings WHERE id=1")
    poll_state = await db.fetch_one("SELECT last_polled_at FROM gmail_poll_state WHERE id=1")
    return GmailIntegrationStatus(**await integration_status((settings or {}).get("haveloc_notification_sender", ""), bool((settings or {}).get("gmail_polling_enabled", False)), (poll_state or {}).get("last_polled_at")))


@router.get("/oauth/start")
async def gmail_oauth_start():
    try:
        url, _ = await create_authorization()
    except (GmailConfigurationError, GmailStorageError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return RedirectResponse(url)


@router.get("/oauth/callback")
async def gmail_oauth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(status_code=400, detail="Gmail authorization was denied or canceled.")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Google did not return a complete Gmail authorization response.")
    try:
        completed = await complete_authorization(code, state)
    except GmailConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except GmailStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except GmailAuthorizationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not completed:
        raise HTTPException(status_code=400, detail="Gmail OAuth state is invalid or expired.")
    return RedirectResponse(os.environ.get("APP_URL", "http://localhost:5173"))


@router.post("/poll")
async def gmail_poll():
    try:
        result = await poll_gmail_once()
    except GmailConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except GmailAuthorizationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from None
    except GmailServiceError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except GmailStorageError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    return {"status": result, "poll_interval_minutes": 2}
