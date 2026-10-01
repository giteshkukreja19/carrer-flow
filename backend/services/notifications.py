"""Opt-in, provider-abstracted WhatsApp notifications for persisted placement data."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lib import db
from lib.reminders import _parse, build_reminders, persist_reminders
from models.placement import PlacementAlert, Reminder

logger = logging.getLogger(__name__)

SUPPORTED_EVENT_TYPES = frozenset({
    "NEW_JOB",
    "APPLICATION_DEADLINE",
    "ROUND_SCHEDULED",
    "ROUND_CHANGED",
    "ROUND_CONFIRMATION",
    "ATTENDANCE_REQUIRED",
    "APPLICATION_STATUS",
})
SUPPORTED_REMINDER_KINDS = frozenset({"application_deadline", "round", "attendance"})
MAX_MESSAGE_LENGTH = 3000
MAX_ATTEMPTS = 5
RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 6 * 60 * 60
CLAIM_LEASE_MINUTES = 10


class NotificationError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NotificationConfigurationError(NotificationError):
    pass


class PermanentNotificationError(NotificationError):
    pass


class TransientNotificationError(NotificationError):
    pass


class NotificationProvider(Protocol):
    async def send_message(self, message: str, *, idempotency_key: str) -> str: ...


@dataclass(frozen=True)
class MetaWhatsAppCloudProvider:
    phone_number_id: str
    recipient_number: str
    api_version: str
    access_token: str = field(repr=False)

    async def send_message(self, message: str, *, idempotency_key: str) -> str:
        return await asyncio.to_thread(self._send_sync, message, idempotency_key)

    def _send_sync(self, message: str, idempotency_key: str) -> str:
        # The stable key is passed to this abstraction for providers that support
        # provider-side idempotency; Meta deduplication is enforced by our durable
        # database claim before requests are made.
        del idempotency_key
        payload = {
            "messaging_product": "whatsapp",
            "to": self.recipient_number,
            "type": "text",
            "text": {"preview_url": False, "body": message},
        }
        request = Request(
            f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=20) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            status = int(exc.code)
            code = f"http_{status}"
            if status in {408, 425, 429} or status >= 500:
                raise TransientNotificationError(code) from None
            raise PermanentNotificationError(code) from None
        except (URLError, TimeoutError, OSError):
            raise TransientNotificationError("network_error") from None
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The request may have been accepted; do not blindly send it again.
            raise PermanentNotificationError("invalid_provider_response") from None

        try:
            message_id = response_payload["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            raise PermanentNotificationError("invalid_provider_response") from None
        if not isinstance(message_id, str) or not message_id.strip():
            raise PermanentNotificationError("invalid_provider_response")
        return message_id


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def whatsapp_requested() -> bool:
    """True only after a user explicitly sets WHATSAPP_ENABLED."""

    return _truthy("WHATSAPP_ENABLED")


def whatsapp_configured() -> bool:
    provider = os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
    version = os.environ.get("WHATSAPP_API_VERSION", "").strip()
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
    recipient = os.environ.get("WHATSAPP_RECIPIENT_NUMBER", "").strip()
    recipient_digits = recipient[1:] if recipient.startswith("+") else recipient
    return bool(
        provider == "meta_cloud"
        and re.fullmatch(r"v[0-9]+\.[0-9]+", version)
        and phone_number_id.isdigit()
        and access_token
        and recipient_digits.isdigit()
    )


def notifications_enabled() -> bool:
    """Respect the existing global notification kill switch."""

    return os.environ.get("NOTIFICATIONS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def whatsapp_enabled() -> bool:
    return whatsapp_requested() and whatsapp_configured() and notifications_enabled()


def notification_status(last_delivery: dict[str, Any] | None = None) -> dict[str, Any]:
    provider = os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
    configured = whatsapp_configured()
    return {
        "configured": configured,
        "enabled": whatsapp_enabled(),
        "provider": provider if provider == "meta_cloud" else None,
        "last_notification_at": (
            (last_delivery or {}).get("sent_at")
            or (last_delivery or {}).get("updated_at")
            if last_delivery else None
        ),
        "last_notification_status": (last_delivery or {}).get("status"),
    }


def _get_provider() -> NotificationProvider:
    if not whatsapp_configured():
        raise NotificationConfigurationError("configuration_missing")
    return MetaWhatsAppCloudProvider(
        phone_number_id=os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip(),
        recipient_number=os.environ["WHATSAPP_RECIPIENT_NUMBER"].strip().removeprefix("+"),
        api_version=os.environ["WHATSAPP_API_VERSION"].strip(),
        access_token=os.environ["WHATSAPP_ACCESS_TOKEN"].strip(),
    )


def _clean_field(value: Any) -> str | None:
    if value is None:
        return None
    text = "".join(
        character if ord(character) >= 32 and ord(character) != 127 else " "
        for character in str(value)
    ).strip()
    text = " ".join(text.split())
    return text[:500] if text else None


def format_alert_message(alert: PlacementAlert | dict[str, Any]) -> str | None:
    """Format only supported, persisted structured fields; never include email body."""

    data = alert.model_dump() if isinstance(alert, PlacementAlert) else alert
    event_type = str(data.get("event_type") or "")
    if event_type not in SUPPORTED_EVENT_TYPES:
        return None

    lines = ["PLACEMENT ALERT"]
    fields = (
        ("Company", data.get("company")),
        ("Role", data.get("role")),
        ("Event", event_type.replace("_", " ")),
        ("Date", data.get("date") or data.get("date_time")),
        ("Time", data.get("time")),
        ("Deadline", data.get("deadline")),
        ("Round", data.get("round")),
        ("Location / mode", data.get("location_mode")),
        ("Status", data.get("application_status") if event_type == "APPLICATION_STATUS" else None),
        ("Action", data.get("required_action")),
    )
    for label, raw_value in fields:
        value = _clean_field(raw_value)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)[:MAX_MESSAGE_LENGTH]


def format_reminder_message(reminder: Reminder | dict[str, Any]) -> str | None:
    data = reminder.model_dump() if isinstance(reminder, Reminder) else reminder
    kind = str(data.get("kind") or "")
    if kind not in SUPPORTED_REMINDER_KINDS:
        return None
    lines = ["PLACEMENT REMINDER"]
    for label, value in (
        ("Reminder", data.get("title")),
        ("Due", data.get("due_at")),
        ("Action", data.get("description")),
    ):
        clean = _clean_field(value)
        if clean:
            lines.append(f"{label}: {clean}")
    return "\n".join(lines)[:MAX_MESSAGE_LENGTH]


def _stable_delivery_id(source_key: str) -> str:
    return "wa-" + hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:32]


async def enqueue_notification(
    *,
    source_key: str,
    source_type: str,
    source_id: str,
    event_type: str,
    message: str,
) -> dict[str, Any] | None:
    """Create one immutable delivery record per alert/reminder source key."""

    if not whatsapp_enabled():
        return None
    now = datetime.now(timezone.utc)
    row = await db.fetch_one(
        """INSERT INTO whatsapp_notifications
             (id, source_key, source_type, source_id, event_type, provider,
              message_body, status, retryable, attempt_count, created_at, updated_at)
           VALUES ($1,$2,$3,$4,$5,'meta_cloud',$6,'pending',FALSE,0,$7,$7)
           ON CONFLICT (source_key) DO NOTHING
           RETURNING *""",
        _stable_delivery_id(source_key),
        source_key,
        source_type,
        source_id,
        event_type,
        message[:MAX_MESSAGE_LENGTH],
        now,
    )
    if row:
        await _safe_activity(
            "whatsapp_notification_queued",
            "A WhatsApp notification was queued.",
            {"source_key": source_key, "event_type": event_type},
        )
        return row
    return await db.fetch_one(
        "SELECT * FROM whatsapp_notifications WHERE source_key=$1",
        source_key,
    )


async def _safe_activity(event_type: str, message: str, metadata: dict[str, Any]) -> None:
    from services.scan_service import log_activity

    try:
        await log_activity(event_type, message, "info", metadata)
    except Exception as exc:
        logger.warning("WhatsApp audit event could not be saved (%s).", type(exc).__name__)


async def queue_alert_notifications() -> int:
    rows = await db.fetch_all(
        """SELECT a.*, m.sender AS source_sender, m.extraction_result
           FROM placement_alerts a
           LEFT JOIN gmail_messages m ON m.message_id=a.gmail_message_id
           WHERE a.event_type = ANY($1::text[])
             AND a.triage_status <> 'acknowledged'
           ORDER BY a.created_at ASC LIMIT 500""",
        sorted(SUPPORTED_EVENT_TYPES),
    )
    queued = 0
    for row in rows:
        extraction = row.get("extraction_result") or {}
        if isinstance(extraction, str):
            try:
                extraction = json.loads(extraction)
            except (TypeError, ValueError):
                extraction = {}
        if not isinstance(extraction, dict):
            extraction = {}
        alert = PlacementAlert(
            id=row["id"],
            gmail_message_id=row["gmail_message_id"],
            source_message_id=row.get("gmail_message_id"),
            source_sender=row.get("source_sender"),
            company=row.get("company"),
            role=row.get("role"),
            event_type=row["event_type"],
            round=extraction.get("round"),
            date=extraction.get("date"),
            time=extraction.get("time"),
            date_time=row.get("date_time"),
            deadline=row.get("deadline"),
            location_mode=row.get("location_mode"),
            required_action=row.get("required_action"),
            application_status=extraction.get("application_status"),
            original_subject=row.get("original_subject") or "",
            received_at=row["received_at"],
            created_at=row.get("created_at"),
            notification_status=row.get("notification_status") or "prepared",
            triage_status=row.get("triage_status") or "new",
        )
        message = format_alert_message(alert)
        if not message:
            continue
        before = await db.fetch_one(
            "SELECT id FROM whatsapp_notifications WHERE source_key=$1",
            f"alert:{alert.id}",
        )
        delivery = await enqueue_notification(
            source_key=f"alert:{alert.id}",
            source_type="alert",
            source_id=alert.id,
            event_type=alert.event_type,
            message=message,
        )
        if delivery and before is None:
            queued += 1
    return queued


def _reminder_is_due(row: dict[str, Any], now: datetime) -> bool:
    if row.get("status") not in {"pending", "upcoming", "action_required"}:
        return False
    due_at = row.get("due_at")
    if due_at is None:
        return row.get("status") == "action_required"
    due = _parse(due_at)
    if due is None:
        return False
    return due <= now


async def sync_reminders_for_notifications(now: datetime | None = None) -> list[Reminder]:
    """Refresh durable Phase 5 reminders from persisted placement records."""

    job_rows = await db.fetch_all("SELECT * FROM jobs ORDER BY last_scanned DESC NULLS LAST LIMIT 500")
    round_rows = await db.fetch_all("SELECT * FROM rounds ORDER BY round_date NULLS LAST LIMIT 500")
    attendance_rows = await db.fetch_all("SELECT * FROM attendance ORDER BY start_time NULLS LAST LIMIT 500")
    alert_rows = await db.fetch_all(
        """SELECT a.*, m.extraction_result
           FROM placement_alerts a
           LEFT JOIN gmail_messages m ON m.message_id=a.gmail_message_id
           ORDER BY a.created_at DESC LIMIT 500"""
    )
    for row in alert_rows:
        extraction = row.get("extraction_result") or {}
        if isinstance(extraction, str):
            try:
                extraction = json.loads(extraction)
            except (TypeError, ValueError):
                extraction = {}
        row["round"] = extraction.get("round") if isinstance(extraction, dict) else None
    candidates = build_reminders(job_rows, round_rows, attendance_rows, alert_rows, now=now)
    return await persist_reminders(candidates)


async def queue_due_reminder_notifications(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rows = await db.fetch_all(
        "SELECT * FROM placement_reminders ORDER BY created_at ASC LIMIT 500"
    )
    queued = 0
    for row in rows:
        if not _reminder_is_due(row, now):
            continue
        message = format_reminder_message(row)
        if not message:
            continue
        source_key = f"reminder:{row['id']}"
        before = await db.fetch_one(
            "SELECT id FROM whatsapp_notifications WHERE source_key=$1",
            source_key,
        )
        delivery = await enqueue_notification(
            source_key=source_key,
            source_type="reminder",
            source_id=row["id"],
            event_type=f"REMINDER_{row['kind'].upper()}",
            message=message,
        )
        if delivery and before is None:
            queued += 1
    return queued


def _retry_delay(attempt_count: int) -> timedelta:
    seconds = min(RETRY_BASE_SECONDS * (2 ** max(attempt_count - 1, 0)), RETRY_MAX_SECONDS)
    return timedelta(seconds=seconds)


async def _set_failed(
    delivery: dict[str, Any],
    error_code: str,
    *,
    retryable: bool,
    now: datetime,
) -> None:
    attempts = int(delivery.get("attempt_count") or 0)
    can_retry = retryable and attempts < MAX_ATTEMPTS
    retry_at = now + _retry_delay(max(attempts, 1)) if can_retry else None
    await db.execute(
        """UPDATE whatsapp_notifications
           SET status='failed', retryable=$2, next_attempt_at=$3, claimed_at=NULL,
               error_code=$4, updated_at=$5
           WHERE id=$1""",
        delivery["id"],
        can_retry,
        retry_at,
        error_code,
        now,
    )
    await _safe_activity(
        "whatsapp_notification_failed",
        "A WhatsApp notification failed.",
        {"source_key": delivery.get("source_key", delivery["id"]), "error_code": error_code, "retryable": can_retry},
    )


async def dispatch_notification(
    delivery_id: str,
    *,
    provider: NotificationProvider | None = None,
    now: datetime | None = None,
) -> str:
    now = now or datetime.now(timezone.utc)
    delivery = await db.fetch_one(
        "SELECT * FROM whatsapp_notifications WHERE id=$1",
        delivery_id,
    )
    if delivery is None:
        return "not_found"
    if delivery.get("status") == "sent":
        return "sent"
    if not whatsapp_requested() or not notifications_enabled():
        return "disabled"
    if not whatsapp_configured():
        await _set_failed(delivery, "configuration_missing", retryable=False, now=now)
        return "failed"

    if provider is None:
        try:
            provider = _get_provider()
        except NotificationConfigurationError as exc:
            await _set_failed(delivery, exc.code, retryable=False, now=now)
            return "failed"

    claimed = await db.fetch_one(
        """UPDATE whatsapp_notifications
           SET status='sending', retryable=FALSE, attempt_count=attempt_count+1,
               claimed_at=$2, updated_at=$2, next_attempt_at=NULL
           WHERE id=$1 AND (
             (status='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= $2))
             OR (status='failed' AND retryable=TRUE AND next_attempt_at <= $2)
           )
           RETURNING *""",
        delivery_id,
        now,
    )
    if claimed is None:
        latest = await db.fetch_one(
            "SELECT status FROM whatsapp_notifications WHERE id=$1",
            delivery_id,
        )
        return (latest or {}).get("status", "not_found")

    try:
        provider_message_id = await provider.send_message(
            claimed["message_body"],
            idempotency_key=claimed["source_key"],
        )
    except TransientNotificationError as exc:
        await _set_failed(claimed, exc.code, retryable=True, now=now)
        return "failed"
    except (PermanentNotificationError, NotificationConfigurationError) as exc:
        await _set_failed(claimed, exc.code, retryable=False, now=now)
        return "failed"
    except Exception as exc:
        # Do not print provider exception text, request headers, or source message content.
        await _set_failed(claimed, "provider_error", retryable=True, now=now)
        logger.warning("WhatsApp provider call failed (%s).", type(exc).__name__)
        return "failed"

    # A provider success followed by a database error has an uncertain local
    # state. Leave the row in 'sending'; the stale-send handler marks it for
    # review instead of blindly retrying and risking a duplicate WhatsApp.
    try:
        persisted = await db.execute(
            """UPDATE whatsapp_notifications
               SET status='sent', retryable=FALSE, sent_at=$2, updated_at=$2,
                   claimed_at=NULL, next_attempt_at=NULL, error_code=NULL,
                   provider_message_id=$3
               WHERE id=$1""",
            delivery_id,
            now,
            provider_message_id,
        )
        if not persisted:
            logger.warning("WhatsApp success state could not be saved.")
            return "sending"
    except Exception as exc:
        logger.warning("WhatsApp success state could not be saved (%s).", type(exc).__name__)
        return "sending"

    await _safe_activity(
        "whatsapp_notification_sent",
        "A WhatsApp notification was accepted by the provider.",
        {"source_key": claimed["source_key"], "provider_message_id": provider_message_id},
    )
    return "sent"

async def _mark_abandoned_sends(now: datetime) -> int:
    cutoff = now - timedelta(minutes=CLAIM_LEASE_MINUTES)
    rows = await db.fetch_all(
        """UPDATE whatsapp_notifications
           SET status='failed', retryable=FALSE, error_code='delivery_outcome_unknown',
               claimed_at=NULL, updated_at=$1
           WHERE status='sending' AND claimed_at <= $2
           RETURNING source_key""",
        now,
        cutoff,
    )
    for row in rows:
        await _safe_activity(
            "whatsapp_notification_failed",
            "A WhatsApp notification has an unknown provider outcome and needs review.",
            {"source_key": row["source_key"], "error_code": "delivery_outcome_unknown", "retryable": False},
        )
    return len(rows)


async def dispatch_pending_notifications(
    *,
    now: datetime | None = None,
    provider: NotificationProvider | None = None,
    limit: int = 25,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    rows = await db.fetch_all(
        """SELECT id FROM whatsapp_notifications
           WHERE status='pending'
              OR (status='failed' AND retryable=TRUE AND next_attempt_at <= $1)
           ORDER BY created_at ASC LIMIT $2""",
        now,
        max(1, min(limit, 100)),
    )
    results = {"sent": 0, "failed": 0, "pending": 0}
    for row in rows:
        result = await dispatch_notification(row["id"], provider=provider, now=now)
        if result in results:
            results[result] += 1
    return results


async def run_notification_cycle(
    *,
    now: datetime | None = None,
    provider: NotificationProvider | None = None,
) -> dict[str, Any]:
    """One bounded worker pass; no DB work or provider call unless explicitly enabled."""

    now = now or datetime.now(timezone.utc)
    if not whatsapp_requested() or not notifications_enabled():
        return {"status": "disabled", "queued": 0, "sent": 0, "failed": 0}
    if not whatsapp_configured():
        pending = await db.fetch_all(
            """SELECT * FROM whatsapp_notifications
               WHERE status='pending'
                  OR (status='failed' AND retryable=TRUE)
               ORDER BY created_at ASC LIMIT 100"""
        )
        for delivery in pending:
            await _set_failed(delivery, "configuration_missing", retryable=False, now=now)
        return {"status": "not_configured", "queued": 0, "sent": 0, "failed": len(pending)}

    await _mark_abandoned_sends(now)
    await sync_reminders_for_notifications(now)
    queued_alerts = await queue_alert_notifications()
    queued_reminders = await queue_due_reminder_notifications(now)
    outcomes = await dispatch_pending_notifications(now=now, provider=provider)
    return {
        "status": "ok",
        "queued": queued_alerts + queued_reminders,
        **outcomes,
    }


async def prepare_notifications(alert: PlacementAlert) -> str:
    """Compatibility wrapper retained for callers of the Phase 5 stub."""

    message = format_alert_message(alert)
    if not message:
        return "unsupported"
    delivery = await enqueue_notification(
        source_key=f"alert:{alert.id}",
        source_type="alert",
        source_id=alert.id,
        event_type=alert.event_type,
        message=message,
    )
    return (delivery or {}).get("status", "disabled")
