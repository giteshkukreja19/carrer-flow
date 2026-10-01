import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from lib import db
from lib.reminders import build_reminders, persist_reminders
from models.placement import (
    ActivityLog, AlertTriageUpdate, AnswerProfile, Application, Attendance, DashboardResponse, GmailIntegrationStatus, Job, PlacementAlert, Round, ScanResponse, SchedulerSettings, WhatsAppNotificationStatus,
)
from services.gmail import integration_status, oauth_configured
from services.llm_classifier import gemini_configured
from services.notifications import notification_status
from services.scan_service import log_activity, scan_haveloc

router = APIRouter()
logger = logging.getLogger(__name__)

DEFAULT_SETTINGS = SchedulerSettings()
DEFAULT_PROFILE = AnswerProfile()


def _job(row: dict[str, Any]) -> Job:
    return Job(id=row["id"], external_id=row["external_id"], company=row["company"], role=row["role"], location=row.get("location"), campus_mode=row.get("campus_mode"), gender=row.get("gender"), batch=row.get("batch"), applicants=row.get("applicants"), salary_ctc=row.get("salary_ctc"), stipend=row.get("stipend"), apply_by=row.get("apply_by"), visit_date=row.get("visit_date"), job_type=row.get("job_type"), application_status=row.get("application_status"), haveloc_status=row["haveloc_status"], source_category=row["source_category"], our_decision=row.get("our_decision") or "Review", last_scanned=row.get("last_scanned"), eligibility=row.get("eligibility") or [], questions=row.get("questions") or [], hiring_process=row.get("hiring_process") or [], job_description=row.get("job_description"), skills=row.get("skills") or [], attachments=row.get("attachments") or [], raw_details=row.get("raw_details") or {})


def _safe_settings(row: dict[str, Any]) -> SchedulerSettings:
    settings = SchedulerSettings(**row)
    return settings.model_copy(update={
        "auto_apply_enabled": False,
        "attendance_automation_enabled": False,
        "email_notifications_enabled": False,
    })


@router.get("/health")
async def health() -> dict[str, Any]:
    whatsapp = notification_status()
    return {
        "status": "ok",
        "database_available": db.pool is not None,
        "database_configured": db.database_configured(),
        "scanner_configured": False,
        "gmail_oauth_configured": oauth_configured(),
        "gemini_configured": gemini_configured(),
        "whatsapp_configured": whatsapp["configured"],
        "whatsapp_enabled": whatsapp["enabled"],
    }


def _placement_alert(row: dict[str, Any]) -> PlacementAlert:
    extraction = row.get("extraction_result") or {}
    if isinstance(extraction, str):
        try:
            import json
            extraction = json.loads(extraction)
        except (TypeError, ValueError):
            extraction = {}
    if not isinstance(extraction, dict):
        extraction = {}
    return PlacementAlert(
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
        original_subject=row["original_subject"],
        received_at=row["received_at"],
        created_at=row.get("created_at"),
        notification_status=row.get("notification_status") or "prepared",
        triage_status=row.get("triage_status") or "new",
    )


@router.get("/dashboard", response_model=DashboardResponse)
async def dashboard() -> DashboardResponse:
    job_rows = await db.fetch_all("SELECT * FROM jobs ORDER BY last_scanned DESC NULLS LAST LIMIT 100")
    application_rows = await db.fetch_all("SELECT * FROM applications ORDER BY last_scanned DESC NULLS LAST LIMIT 100")
    round_rows = await db.fetch_all("SELECT * FROM rounds ORDER BY round_date NULLS LAST LIMIT 100")
    attendance_rows = await db.fetch_all("SELECT * FROM attendance ORDER BY start_time NULLS LAST LIMIT 100")
    alert_rows = await db.fetch_all(
        """SELECT a.*, m.sender AS source_sender, m.extraction_result
           FROM placement_alerts a
           LEFT JOIN gmail_messages m ON m.message_id=a.gmail_message_id
           ORDER BY a.received_at DESC LIMIT 100"""
    )
    jobs = [_job(row) for row in job_rows]
    applications = [Application(**row) for row in application_rows]
    rounds = [Round(**row) for row in round_rows]
    attendance = [Attendance(**row) for row in attendance_rows]
    alerts = [_placement_alert(row) for row in alert_rows]

    candidates = build_reminders(job_rows, round_rows, attendance_rows, [a.model_dump() for a in alerts])
    try:
        reminders = await persist_reminders(candidates)
    except Exception as exc:
        # Optional reminder persistence must not make the read-only dashboard unavailable.
        logger.warning("Could not refresh placement reminders (%s).", type(exc).__name__)
        reminders = candidates

    activity = [
        ActivityLog(**row)
        for row in await db.fetch_all("SELECT * FROM activity_log ORDER BY created_at DESC LIMIT 100")
    ]
    try:
        last_notification = await db.fetch_one(
            "SELECT status, sent_at, updated_at FROM whatsapp_notifications ORDER BY updated_at DESC LIMIT 1"
        )
    except Exception as exc:
        logger.warning("Could not read WhatsApp notification status (%s).", type(exc).__name__)
        last_notification = None
    whatsapp = WhatsAppNotificationStatus(**notification_status(last_notification))
    settings_row = await db.fetch_one("SELECT * FROM scheduler_settings WHERE id=1")
    profile_row = await db.fetch_one("SELECT * FROM answer_profile WHERE id=1")
    poll_state = await db.fetch_one("SELECT last_polled_at FROM gmail_poll_state WHERE id=1")
    settings = _safe_settings(settings_row) if settings_row else DEFAULT_SETTINGS
    integration = GmailIntegrationStatus(**await integration_status(
        settings.haveloc_notification_sender,
        settings.gmail_polling_enabled,
        (poll_state or {}).get("last_polled_at"),
    ))
    last_scan = activity[0].created_at if activity and activity[0].event_type.startswith("scan_") else None
    return DashboardResponse(
        jobs=jobs,
        applications=applications,
        rounds=rounds,
        attendance=attendance,
        activity=activity,
        reminders=reminders,
        alerts=alerts,
        whatsapp_status=whatsapp,
        integration_status=integration,
        settings=settings,
        answer_profile=AnswerProfile(**profile_row) if profile_row else DEFAULT_PROFILE,
        database_available=db.pool is not None,
        database_configured=db.database_configured(),
        scanner_configured=False,
        last_scan_at=last_scan,
    )

@router.get("/settings", response_model=SchedulerSettings)
async def get_settings() -> SchedulerSettings:
    row = await db.fetch_one("SELECT * FROM scheduler_settings WHERE id=1")
    return _safe_settings(row) if row else DEFAULT_SETTINGS


@router.put("/settings", response_model=SchedulerSettings)
async def update_settings(settings: SchedulerSettings) -> SchedulerSettings:
    # Safety gates are server-enforced and cannot be enabled from this read-only MVP.
    if db.pool is None:
        raise HTTPException(status_code=503, detail="Settings cannot be saved until PostgreSQL is available.")
    safe = settings.model_copy(update={"auto_apply_enabled": False, "attendance_automation_enabled": False, "email_notifications_enabled": False})
    await db.execute("INSERT INTO scheduler_settings (id, scan_interval_minutes, notifications_enabled, auto_apply_enabled, attendance_automation_enabled, email_notifications_enabled, haveloc_notification_sender, gmail_polling_enabled) VALUES (1,$1,$2,FALSE,FALSE,FALSE,$3,$4) ON CONFLICT (id) DO UPDATE SET scan_interval_minutes=EXCLUDED.scan_interval_minutes, notifications_enabled=EXCLUDED.notifications_enabled, haveloc_notification_sender=EXCLUDED.haveloc_notification_sender, gmail_polling_enabled=EXCLUDED.gmail_polling_enabled", safe.scan_interval_minutes, safe.notifications_enabled, safe.haveloc_notification_sender.strip(), safe.gmail_polling_enabled)
    return safe


@router.get("/answer-profile", response_model=AnswerProfile)
async def get_answer_profile() -> AnswerProfile:
    row = await db.fetch_one("SELECT * FROM answer_profile WHERE id=1")
    return AnswerProfile(**row) if row else DEFAULT_PROFILE


@router.put("/answer-profile", response_model=AnswerProfile)
async def update_answer_profile(profile: AnswerProfile) -> AnswerProfile:
    if db.pool is None:
        raise HTTPException(status_code=503, detail="The answer profile cannot be saved until PostgreSQL is available.")
    await db.execute("INSERT INTO answer_profile (id, full_name, github_url, linkedin_url, portfolio_url, internship_experience, preferred_role, relocation_willingness, physical_attendance_willingness) VALUES (1,$1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (id) DO UPDATE SET full_name=EXCLUDED.full_name, github_url=EXCLUDED.github_url, linkedin_url=EXCLUDED.linkedin_url, portfolio_url=EXCLUDED.portfolio_url, internship_experience=EXCLUDED.internship_experience, preferred_role=EXCLUDED.preferred_role, relocation_willingness=EXCLUDED.relocation_willingness, physical_attendance_willingness=EXCLUDED.physical_attendance_willingness", profile.full_name, profile.github_url, profile.linkedin_url, profile.portfolio_url, profile.internship_experience, profile.preferred_role, profile.relocation_willingness, profile.physical_attendance_willingness)
    return profile


@router.patch("/alerts/{alert_id}/triage", response_model=PlacementAlert)
async def update_alert_triage(alert_id: str, update: AlertTriageUpdate) -> PlacementAlert:
    row = await db.fetch_one(
        """SELECT a.*, m.sender AS source_sender, m.extraction_result
           FROM placement_alerts a
           LEFT JOIN gmail_messages m ON m.message_id=a.gmail_message_id
           WHERE a.id=$1""",
        alert_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Placement alert not found.")

    current = row.get("triage_status") or "new"
    requested = update.status
    allowed = (current == "new" and requested == "review") or (
        current == "review" and requested == "acknowledged"
    ) or (current == "acknowledged" and requested == "acknowledged")
    if not allowed:
        raise HTTPException(status_code=409, detail=f"Cannot move alert from {current} to {requested}.")

    if current != requested:
        changed = await db.fetch_one(
            """UPDATE placement_alerts SET triage_status=$2
               WHERE id=$1 AND triage_status=$3 RETURNING id""",
            alert_id,
            requested,
            current,
        )
        if not changed:
            raise HTTPException(status_code=409, detail="Placement alert triage state changed; refresh and retry.")
        event_type = "alert_reviewed" if requested == "review" else "alert_acknowledged"
        await log_activity(
            event_type,
            "A placement alert was moved to " + requested + ".",
            "info",
            {"alert_id": alert_id, "event_type": row.get("event_type")},
        )
        row["triage_status"] = requested
    return _placement_alert(row)

@router.post("/scan", response_model=ScanResponse)
async def trigger_scan() -> ScanResponse:
    return await scan_haveloc()
