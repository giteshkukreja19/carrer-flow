"""Deterministic placement reminder candidates and persistent reminder synchronization."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from models.placement import Reminder


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        parsed = None
        # Only parse unambiguous month-name dates. Numeric dates are left as raw
        # text because their day/month order cannot be known safely.
        for fmt in (
            "%d %b %Y %H:%M",
            "%d %B %Y %H:%M",
            "%b %d, %Y %H:%M",
            "%B %d, %Y %H:%M",
            "%d %b %Y %I:%M %p",
            "%d %B %Y %I:%M %p",
            "%b %d, %Y %I:%M %p",
            "%B %d, %Y %I:%M %p",
            "%d %b %Y %I %p",
            "%d %B %Y %I %p",
            "%d %b %Y",
            "%d %B %Y",
            "%b %d, %Y",
            "%B %d, %Y",
        ):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable_reminder_id(kind: str, source_type: str, source_id: str) -> str:
    key = "\0".join((kind, source_type, source_id)).encode("utf-8")
    return "rem-" + hashlib.sha256(key).hexdigest()[:32]


def _within_window(value: Any, now: datetime) -> bool:
    due = _parse(value)
    return due is not None and now <= due <= now + timedelta(hours=48)


def _source(source_type: str, source_id: str) -> tuple[str, str]:
    if source_type == "gmail" and source_id.startswith("gmail:"):
        return "placement_event", source_id.removeprefix("gmail:")
    return source_type, source_id


def _candidate(
    kind: str,
    title: str,
    description: str,
    due_at: Any,
    status: str,
    severity: str,
    source_type: str,
    source_id: str,
) -> Reminder:
    return Reminder(
        id=_stable_reminder_id(kind, source_type, source_id),
        kind=kind,
        title=title,
        description=description,
        due_at=str(due_at) if due_at is not None else None,
        status=status,
        severity=severity,
        source_id=source_id,
        source_type=source_type,
    )


def build_reminders(
    job_rows: Iterable[dict[str, Any]],
    round_rows: Iterable[dict[str, Any]],
    attendance_rows: Iterable[dict[str, Any]],
    alert_rows: Iterable[dict[str, Any]] = (),
    *,
    now: datetime | None = None,
) -> list[Reminder]:
    """Build stable candidates from known records; no dates or actions are inferred."""

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    candidates: dict[tuple[str, str, str], Reminder] = {}

    def add(reminder: Reminder) -> None:
        key = (reminder.kind, reminder.source_type, reminder.source_id)
        candidates[key] = reminder

    for job in job_rows:
        due = job.get("apply_by")
        if not due or not _within_window(due, now):
            continue
        source_type = "gmail" if job.get("source_category") == "gmail" else "job"
        source_id = str(job.get("external_id") or job.get("id") or "")
        source_type, source_id = _source(source_type, source_id)
        add(_candidate(
            "application_deadline",
            f"Apply: {job.get('company') or 'Company not stated'}",
            f"{job.get('role') or 'Role not stated'} application deadline is approaching.",
            due,
            "pending",
            "rose",
            source_type,
            source_id,
        ))

    for rnd in round_rows:
        due = rnd.get("round_date")
        if not due or not _within_window(due, now):
            continue
        gmail_source = rnd.get("source_gmail_message_id")
        source_type, source_id = _source(
            "gmail" if gmail_source else "round",
            str(gmail_source or rnd.get("id") or ""),
        )
        add(_candidate(
            "round",
            f"Round: {rnd.get('company') or 'Company not stated'}",
            f"{rnd.get('round_name') or 'Placement round'} is scheduled soon.",
            " ".join(str(v) for v in (rnd.get("round_date"), rnd.get("round_time")) if v) or due,
            "upcoming",
            "amber",
            source_type,
            source_id,
        ))

    for item in attendance_rows:
        action = item.get("action_required")
        if not action:
            continue
        add(_candidate(
            "attendance",
            f"Attendance: {item.get('company') or 'Company not stated'}",
            str(action),
            item.get("start_time"),
            "action_required",
            "rose",
            "attendance",
            str(item.get("id") or ""),
        ))

    for alert in alert_rows:
        source_id = str(alert.get("gmail_message_id") or alert.get("source_message_id") or "")
        if not source_id:
            continue
        source_type = "placement_event"
        event_type = str(alert.get("event_type") or "")
        company = alert.get("company") or "Company not stated"
        role = alert.get("role") or "Role not stated"
        due_value = alert.get("deadline") if event_type == "APPLICATION_DEADLINE" else alert.get("date_time")
        if event_type == "APPLICATION_DEADLINE" and due_value and _within_window(due_value, now):
            add(_candidate(
                "application_deadline",
                f"Apply: {company}",
                f"{role} application deadline is approaching.",
                due_value,
                "pending",
                "rose",
                source_type,
                source_id,
            ))
        elif event_type in {"ROUND_SCHEDULED", "ROUND_CHANGED", "ROUND_CONFIRMATION"}:
            if due_value and _within_window(due_value, now):
                add(_candidate(
                    "round",
                    f"Round: {company}",
                    f"{alert.get('round') or 'Placement round'} is scheduled soon.",
                    due_value,
                    "upcoming",
                    "amber",
                    source_type,
                    source_id,
                ))
        elif event_type == "ATTENDANCE_REQUIRED":
            action = alert.get("required_action") or "Review the attendance instructions in the source email."
            add(_candidate(
                "attendance",
                f"Attendance: {company}",
                str(action),
                due_value,
                "action_required",
                "rose",
                source_type,
                source_id,
            ))

    result = list(candidates.values())
    result.sort(key=lambda item: _parse(item.due_at) or datetime.max.replace(tzinfo=timezone.utc))
    return result[:100]


async def persist_reminders(reminders: Iterable[Reminder]) -> list[Reminder]:
    """Idempotently upsert reminder state and audit only newly created reminders."""

    from lib import db
    from services.scan_service import log_activity

    if db.pool is None:
        return list(reminders)

    for reminder in reminders:
        source_type = reminder.source_type or "placement"
        existing = await db.fetch_one(
            "SELECT id FROM placement_reminders WHERE kind=$1 AND source_type=$2 AND source_id=$3",
            reminder.kind,
            source_type,
            reminder.source_id,
        )
        now = datetime.now(timezone.utc)
        row = await db.fetch_one(
            """INSERT INTO placement_reminders
                 (id, kind, title, description, due_at, status, severity, source_id, source_type, created_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               ON CONFLICT (kind, source_type, source_id) DO UPDATE SET
                 title=EXCLUDED.title, description=EXCLUDED.description,
                 due_at=EXCLUDED.due_at, severity=EXCLUDED.severity
               WHERE placement_reminders.title IS DISTINCT FROM EXCLUDED.title
                  OR placement_reminders.description IS DISTINCT FROM EXCLUDED.description
                  OR placement_reminders.due_at IS DISTINCT FROM EXCLUDED.due_at
                  OR placement_reminders.severity IS DISTINCT FROM EXCLUDED.severity
               RETURNING *""",
            reminder.id,
            reminder.kind,
            reminder.title,
            reminder.description,
            reminder.due_at,
            reminder.status,
            reminder.severity,
            reminder.source_id,
            source_type,
            now,
        )
        if row and existing is None:
            await log_activity(
                "reminder_created",
                "A placement reminder was created.",
                "info",
                {"kind": reminder.kind, "source_type": source_type, "source_id": reminder.source_id},
            )

    rows = await db.fetch_all(
        "SELECT * FROM placement_reminders ORDER BY due_at NULLS LAST, created_at DESC LIMIT 100"
    )
    return [Reminder(**row) for row in rows] if rows else list(reminders)
