"""Conservative materialization of explicitly extracted placement events."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from lib import db
from services.llm_classifier import EmailExtraction


def _stable_id(kind: str, message_id: str) -> str:
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:32]
    return f"{kind}-{digest}"


def _activity(event_type: str, message: str, message_id: str, record_id: str) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "message": message,
        "severity": "info",
        "metadata": {"gmail_message_id": message_id, "record_id": record_id},
    }


async def materialize_placement_event(
    message_id: str, extraction: EmailExtraction
) -> list[dict[str, Any]]:
    """Create or update only records that have explicit, unambiguous source fields."""

    events: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)

    if extraction.event_type in {"NEW_JOB", "APPLICATION_DEADLINE"} and extraction.company and extraction.role:
        record_id = _stable_id("gmail-job", message_id)
        inserted = await db.fetch_one(
            """INSERT INTO jobs
                 (id, external_id, company, role, apply_by, application_status,
                  haveloc_status, source_category, our_decision, last_scanned,
                  raw_details)
               VALUES ($1,$2,$3,$4,$5,NULL,'Email alert','gmail','Review',$6,$7::jsonb)
               ON CONFLICT (external_id) DO NOTHING
               RETURNING id""",
            record_id,
            f"gmail:{message_id}",
            extraction.company,
            extraction.role,
            extraction.deadline,
            now,
            db.json_text({"gmail_message_id": message_id, "event_type": extraction.event_type}),
        )
        if inserted:
            events.append(_activity("job_created", "A placement job record was created from an email.", message_id, record_id))

    if extraction.event_type == "ROUND_SCHEDULED" and extraction.company and extraction.round:
        record_id = _stable_id("gmail-round", message_id)
        inserted = await db.fetch_one(
            """INSERT INTO rounds
                 (id, company, round_name, round_date, round_time, round_status,
                  confirmation_status, last_scanned, source_gmail_message_id)
               VALUES ($1,$2,$3,$4,$5,'Scheduled','Unknown',$6,$7)
               ON CONFLICT (id) DO UPDATE SET
                 round_date=EXCLUDED.round_date,
                 round_time=EXCLUDED.round_time,
                 last_scanned=EXCLUDED.last_scanned
               WHERE rounds.round_date IS DISTINCT FROM EXCLUDED.round_date
                  OR rounds.round_time IS DISTINCT FROM EXCLUDED.round_time
               RETURNING id""",
            record_id,
            extraction.company,
            extraction.round,
            extraction.date,
            extraction.time,
            now,
            message_id,
        )
        if inserted:
            events.append(_activity("round_updated", "A scheduled placement round was recorded.", message_id, record_id))

    if extraction.event_type == "ROUND_CHANGED" and extraction.company and extraction.round:
        matches = await db.fetch_all(
            "SELECT id, round_date, round_time FROM rounds WHERE company=$1 AND round_name=$2 ORDER BY id LIMIT 2",
            extraction.company,
            extraction.round,
        )
        # A precise company + round name must identify exactly one existing record.
        if len(matches) == 1:
            current = matches[0]
            changes = (
                (extraction.date is not None and extraction.date != current.get("round_date"))
                or (extraction.time is not None and extraction.time != current.get("round_time"))
            )
            if changes:
                updated = await db.fetch_one(
                    """UPDATE rounds SET
                         round_date=COALESCE($2,round_date),
                         round_time=COALESCE($3,round_time),
                         last_scanned=$4
                       WHERE id=$1 RETURNING id""",
                    current["id"],
                    extraction.date,
                    extraction.time,
                    now,
                )
                if updated:
                    events.append(_activity("round_updated", "A placement round schedule was updated from an email.", message_id, current["id"]))

    if (
        extraction.event_type == "APPLICATION_STATUS"
        and extraction.company
        and extraction.role
        and extraction.application_status
    ):
        matches = await db.fetch_all(
            "SELECT id FROM applications WHERE company=$1 AND role=$2 ORDER BY id LIMIT 2",
            extraction.company,
            extraction.role,
        )
        # Never guess which application is meant when the company/role is ambiguous.
        if len(matches) == 1:
            updated = await db.fetch_one(
                """UPDATE applications SET application_status=$2,last_scanned=$3
                   WHERE id=$1 AND application_status IS DISTINCT FROM $2
                   RETURNING id""",
                matches[0]["id"],
                extraction.application_status,
                now,
            )
            if updated:
                events.append(_activity("application_status_updated", "An application status was updated from an email.", message_id, matches[0]["id"]))

    # ATTENDANCE_REQUIRED stays an alert/reminder. It deliberately does not create
    # an attendance record or mark presence.
    return events
