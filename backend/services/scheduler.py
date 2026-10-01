import asyncio
import logging

from services.ingestion import poll_gmail_once
from services.notifications import run_notification_cycle

logger = logging.getLogger(__name__)
WHATSAPP_WORKER_INTERVAL_SECONDS = 60


async def gmail_poll_loop() -> None:
    while True:
        try:
            await asyncio.sleep(120)
            await poll_gmail_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gmail placement poll failed")
            await asyncio.sleep(60)


async def whatsapp_notification_loop() -> None:
    """Poll durable delivery state at a bounded interval; never spin on failure."""

    while True:
        try:
            await asyncio.sleep(WHATSAPP_WORKER_INTERVAL_SECONDS)
            await run_notification_cycle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Provider errors and request details are intentionally omitted.
            logger.warning("WhatsApp notification worker failed (%s).", type(exc).__name__)
