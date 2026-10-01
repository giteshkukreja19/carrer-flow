"""Read-only Gmail ingestion and retryable placement email classification."""

from __future__ import annotations

import logging
import hashlib
from datetime import datetime, timezone
from typing import Any

from lib import db
from models.placement import PlacementAlert
from services.gmail import (
    GmailStorageError,
    fetch_messages,
    integration_status,
    oauth_configured,
    validate_sender,
)
from services.llm_classifier import EmailExtraction, classify_email, gemini_configured
from services.scan_service import log_activity
from services.tracking import materialize_placement_event

logger = logging.getLogger(__name__)
CLASSIFICATION_BATCH_SIZE = 50
RETRYABLE_STATUSES = ("pending_classification", "received", "failed")


async def poll_gmail_once() -> str:
    """Store exact-sender mail first, then classify pending messages when configured."""

    try:
        settings = await db.fetch_one(
            "SELECT haveloc_notification_sender, gmail_polling_enabled "
            "FROM scheduler_settings WHERE id=1"
        )
    except Exception:
        raise GmailStorageError("PostgreSQL could not read Gmail polling settings.") from None
    sender = ((settings or {}).get("haveloc_notification_sender") or "").strip()
    polling_enabled = bool((settings or {}).get("gmail_polling_enabled", False))
    if not polling_enabled or not sender or not oauth_configured():
        return "disabled"
    sender = validate_sender(sender)

    status = await integration_status(sender, polling_enabled, None)
    if not status["gmail_connected"]:
        return "not_connected"

    try:
        previous = await db.fetch_one("SELECT last_polled_at FROM gmail_poll_state WHERE id=1")
    except Exception:
        raise GmailStorageError("PostgreSQL could not read Gmail polling progress.") from None
    since = (previous or {}).get("last_polled_at")
    messages = await fetch_messages(sender, since)

    ingested = 0
    for message in messages:
        try:
            inserted = await db.fetch_one(
                "INSERT INTO gmail_messages "
                "(message_id, thread_id, sender, subject, received_at, processing_status, body_text) "
                "VALUES ($1,$2,$3,$4,$5,'pending_classification',$6) "
                "ON CONFLICT (message_id) DO NOTHING RETURNING message_id",
                message.message_id,
                message.thread_id,
                message.sender,
                message.subject,
                message.received_at,
                message.body,
            )
        except Exception:
            raise GmailStorageError("PostgreSQL could not store a received Gmail message.") from None
        if not inserted:
            continue
        try:
            await log_activity(
                "email_received",
                "Haveloc notification email stored for review.",
                "info",
                {"gmail_message_id": message.message_id},
            )
        except Exception:
            raise GmailStorageError("PostgreSQL could not write the Gmail ingestion audit record.") from None
        ingested += 1

    try:
        updated = await db.execute(
            "UPDATE gmail_poll_state SET last_polled_at=$1 WHERE id=1",
            datetime.now(timezone.utc),
        )
    except Exception:
        raise GmailStorageError("PostgreSQL could not save Gmail polling progress.") from None
    if not updated:
        raise GmailStorageError("PostgreSQL could not save Gmail polling progress.")

    # Gmail polling is independent of AI setup. Without a provider these rows stay
    # pending; after setup, each poll retries them without fetching them again.
    await process_pending_gmail_messages()
    return f"ingested:{ingested}"


async def process_pending_gmail_messages(limit: int = CLASSIFICATION_BATCH_SIZE) -> int:
    """Classify retryable mail, materialize safe records, and leave failures retryable."""

    provider_available = gemini_configured()
    try:
        rows = await db.fetch_all(
            """SELECT message_id, sender, subject, received_at, body_text,
                      processing_status, extraction_result
               FROM gmail_messages
               WHERE processing_status = ANY($1::text[])
                  OR (processing_status='classified' AND tracking_status='pending')
               ORDER BY received_at ASC
               LIMIT $2""",
            list(RETRYABLE_STATUSES),
            max(1, min(limit, 500)),
        )
    except Exception as exc:
        logger.warning("Could not load pending Gmail messages (%s).", type(exc).__name__)
        return 0

    classified = 0
    for message in rows:
        message_id = message.get("message_id")
        was_classified = message.get("processing_status") == "classified"
        classification_persisted = False
        try:
            if was_classified:
                extraction = EmailExtraction.model_validate(message.get("extraction_result") or {})
            else:
                if not provider_available:
                    continue
                extraction = await classify_email(
                    subject=message.get("subject") or "",
                    body=message.get("body_text") or "",
                    received_at=message["received_at"],
                )
                persisted = await _persist_classification(message, extraction)
                if not persisted:
                    continue
                classification_persisted = True
                classified += 1
                await _safe_activity(
                    "alert_created",
                    "A placement alert was created from a classified email.",
                    "info",
                    {"gmail_message_id": message_id, "event_type": extraction.event_type},
                )
                await _safe_activity(
                    "email_classified",
                    f"Placement email classified as {extraction.event_type}.",
                    "info",
                    {"gmail_message_id": message_id, "event_type": extraction.event_type},
                )

            for event in await materialize_placement_event(message_id, extraction):
                await _safe_activity(
                    event["event_type"],
                    event["message"],
                    event["severity"],
                    event["metadata"],
                )
            tracked = await db.execute(
                """UPDATE gmail_messages SET tracking_status='complete'
                   WHERE message_id=$1 AND processing_status='classified'""",
                message_id,
            )
            if not tracked:
                raise RuntimeError("tracking state could not be saved")
        except Exception as exc:
            # Provider error messages and email content are deliberately excluded.
            logger.warning("Placement email processing failed (%s).", type(exc).__name__)
            tracking_failed = was_classified or classification_persisted
            if message_id and not tracking_failed:
                try:
                    await db.execute(
                        """UPDATE gmail_messages
                           SET processing_status='pending_classification',
                               processing_error='Classification failed; message is safe to retry.'
                           WHERE message_id=$1
                             AND processing_status = ANY($2::text[])""",
                        message_id,
                        list(RETRYABLE_STATUSES),
                    )
                except Exception as update_exc:
                    logger.warning("Could not preserve a retryable Gmail message (%s).", type(update_exc).__name__)
            if message_id:
                await _safe_activity(
                    "placement_tracking_failed" if tracking_failed else "email_classification_failed",
                    "Placement tracking failed; the saved message remains safe to retry."
                    if tracking_failed else "Email classification failed; the message remains safe to retry.",
                    "error",
                    {"gmail_message_id": message_id},
                )
    return classified

async def _persist_classification(
    message: dict[str, Any], extraction: EmailExtraction
) -> bool:
    """Atomically store the extraction and unique alert linked to its Gmail message."""

    message_id = message["message_id"]
    alert = _build_alert(message_id, message["received_at"], extraction)
    extraction_json = db.json_text(extraction.model_dump(mode="json"))
    now = datetime.now(timezone.utc)

    # The conditional transition and UNIQUE(gmail_message_id) make retries idempotent.
    row = await db.fetch_one(
        """WITH pending_message AS (
             UPDATE gmail_messages
             SET processing_status='classified', classification_type=$2,
                 extraction_result=$3::jsonb, tracking_status='pending',
                 processing_error=NULL, processed_at=$4
             WHERE message_id=$1
               AND processing_status = ANY($5::text[])
               AND NOT EXISTS (
                   SELECT 1 FROM placement_alerts WHERE gmail_message_id=$1
               )
             RETURNING message_id, received_at
           ), inserted_alert AS (
             INSERT INTO placement_alerts
               (id, gmail_message_id, company, role, event_type, date_time, deadline,
                location_mode, required_action, original_subject, received_at,
                notification_status, triage_status, created_at)
             SELECT $6, pending_message.message_id, $7, $8, $2, $9, $10,
                    $11, $12, $13, pending_message.received_at,
                    'prepared', 'new', $14
             FROM pending_message
             ON CONFLICT (gmail_message_id) DO NOTHING
             RETURNING gmail_message_id
           )
           SELECT gmail_message_id FROM inserted_alert""",
        message_id,
        extraction.event_type,
        extraction_json,
        now,
        list(RETRYABLE_STATUSES),
        alert.id,
        alert.company,
        alert.role,
        alert.date_time,
        alert.deadline,
        alert.location_mode,
        alert.required_action,
        alert.original_subject,
        now,
    )
    return bool(row)


def _build_alert(
    message_id: str, received_at: datetime, extraction: EmailExtraction
) -> PlacementAlert:
    date_time = " ".join(value for value in (extraction.date, extraction.time) if value) or None
    stable_id = "alert-" + hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    return PlacementAlert(
        id=stable_id,
        gmail_message_id=message_id,
        source_message_id=message_id,
        company=extraction.company,
        role=extraction.role,
        event_type=extraction.event_type,
        round=extraction.round,
        date=extraction.date,
        time=extraction.time,
        date_time=date_time,
        deadline=extraction.deadline,
        location_mode=extraction.location_mode,
        required_action=extraction.required_action,
        application_status=extraction.application_status,
        original_subject=extraction.original_subject,
        received_at=received_at,
        created_at=datetime.now(timezone.utc),
    )


async def _safe_activity(event_type: str, message: str, severity: str, metadata: dict[str, Any]) -> None:
    try:
        await log_activity(event_type, message, severity, metadata)
    except Exception as exc:
        logger.warning("Could not write Gmail classification audit entry (%s).", type(exc).__name__)
