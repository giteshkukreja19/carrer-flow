from datetime import datetime, timezone
import hashlib
import uuid

from automation.haveloc import HavelocClient, scan_enabled, scanner_configured
from lib import db
from models.placement import ScanResponse


async def log_activity(event_type: str, message: str, severity: str = "info", metadata: dict | None = None):
    await db.execute(
        "INSERT INTO activity_log (id, event_type, message, severity, created_at, metadata) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
        str(uuid.uuid4()), event_type, message, severity, datetime.now(timezone.utc), db.json_text(metadata or {}),
    )


async def scan_haveloc() -> ScanResponse:
    now = datetime.now(timezone.utc)
    if not scanner_configured():
        state = "not_configured" if _scan_requested() else "disabled"
        message = (
            "Haveloc scanning was requested but page URLs or a session cookie are missing."
            if state == "not_configured"
            else "Haveloc scanning is disabled by default."
        )
        await log_activity("scan_" + state, message, "warning", {})
        return ScanResponse(status=state, message=message, attention_required=state == "not_configured", last_scan_at=now)
    if db.pool is None:
        return ScanResponse(status="storage_unavailable", message="Haveloc results were not fetched because PostgreSQL is unavailable.", attention_required=True, last_scan_at=now)

    result = await HavelocClient().scan()
    if result.get("status") != "ok":
        status = str(result.get("status", "error"))
        message = str(result.get("message", "Haveloc scan did not complete."))
        await log_activity("scan_" + status, message, "warning", {})
        return ScanResponse(status=status, message=message, attention_required=bool(result.get("attention_required", status != "disabled")), last_scan_at=now)

    try:
        counts = await _persist_scan(result, now)
    except Exception as exc:
        await log_activity("scan_error", "Haveloc read completed, but local placement records could not be saved.", "error", {"error_type": type(exc).__name__})
        return ScanResponse(status="storage_error", message="Haveloc data could not be saved locally.", attention_required=True, last_scan_at=now)

    await log_activity("scan_completed", "Haveloc jobs, details, rounds, and participation status were read.", "info", counts)
    return ScanResponse(status="ok", message=str(result.get("message")), jobs_found=counts["jobs"], applications_found=0, rounds_found=counts["rounds"], attention_required=False, last_scan_at=now)


def _scan_requested() -> bool:
    return scan_enabled()


def _stable_id(*parts: object) -> str:
    value = "\x1f".join("" if part is None else str(part).strip().casefold() for part in parts)
    return "hvl_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


async def _persist_scan(result: dict, scanned_at: datetime) -> dict[str, int]:
    jobs = result.get("jobs") if isinstance(result.get("jobs"), list) else []
    details_pages = result.get("job_details") if isinstance(result.get("job_details"), list) else []
    participation = result.get("participation") if isinstance(result.get("participation"), dict) else {}
    job_ids: dict[tuple[str, str], list[str]] = {}
    jobs_persisted = 0
    for job in jobs:
        if not isinstance(job, dict) or not job.get("company") or not job.get("role"):
            continue
        company, role = str(job["company"]), str(job["role"])
        external_id = _stable_id("haveloc-job", *(job.get(field) for field in sorted(job)))
        job_id = external_id
        job_ids.setdefault((company.casefold(), role.casefold()), []).append(job_id)
        await db.execute(
            """INSERT INTO jobs (id, external_id, company, role, salary_ctc, stipend, apply_by, visit_date,
                   haveloc_status, source_category, our_decision, last_scanned)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'haveloc','Review',$10)
               ON CONFLICT (external_id) DO UPDATE SET company=EXCLUDED.company, role=EXCLUDED.role,
                   salary_ctc=EXCLUDED.salary_ctc, stipend=EXCLUDED.stipend, apply_by=EXCLUDED.apply_by,
                   visit_date=EXCLUDED.visit_date, haveloc_status=EXCLUDED.haveloc_status,
                   last_scanned=EXCLUDED.last_scanned""",
            job_id, external_id, company, role, job.get("salary_ctc"), job.get("stipend"),
            job.get("apply_by"), job.get("visit_date"), job.get("status") or "Unknown", scanned_at,
        )
        jobs_persisted += 1

    round_count = 0
    for page in details_pages:
        details = page.get("details") if isinstance(page, dict) else None
        if not isinstance(details, dict):
            continue
        company, role = details.get("company"), details.get("role")
        matching_job_ids = job_ids.get((str(company or "").casefold(), str(role or "").casefold()), [])
        job_id = matching_job_ids[0] if len(matching_job_ids) == 1 else None
        if not company or not role or not job_id:
            continue
        await db.execute(
            """UPDATE jobs SET job_description=$3, eligibility=$4::jsonb, hiring_process=$5::jsonb,
                   skills=$6::jsonb, attachments=$7::jsonb, raw_details=$8::jsonb, last_scanned=$9
               WHERE id=$1 AND company=$2""",
            job_id, company, details.get("job_description"), db.json_text(details.get("eligibility") or []),
            db.json_text(details.get("hiring_process") or []), db.json_text(details.get("skills") or []),
            db.json_text(details.get("attachments") or []), db.json_text(details), scanned_at,
        )
        for round_item in details.get("hiring_process") or []:
            if not isinstance(round_item, dict) or not round_item.get("name"):
                continue
            round_name = str(round_item["name"])
            round_id = _stable_id("haveloc-round", company, role, round_name)
            await _upsert_round(round_id, job_id, str(company), round_name, round_item.get("status") or "Unknown", None, scanned_at)
            round_count += 1

    for item in participation.get("rounds", []) if isinstance(participation.get("rounds"), list) else []:
        if not isinstance(item, dict) or not item.get("company") or not item.get("round"):
            continue
        company, role, round_name = str(item["company"]), str(item.get("role") or "Unknown"), str(item["round"])
        matching_job_ids = job_ids.get((company.casefold(), role.casefold()), [])
        job_id = matching_job_ids[0] if len(matching_job_ids) == 1 else None
        round_id = _stable_id("haveloc-round", company, role, round_name)
        meta = item.get("meta") if isinstance(item.get("meta"), list) else []
        event_time = str(meta[0]) if meta else None
        await _upsert_round(round_id, job_id, company, round_name, "Unknown", item.get("status"), scanned_at, event_time)
        round_count += 1

    attendance_items = participation.get("attendance", []) if isinstance(participation.get("attendance"), list) else []
    for item in attendance_items:
        if not isinstance(item, dict) or not item.get("company") or not item.get("round"):
            continue
        company, role, round_name = str(item["company"]), str(item.get("role") or "Unknown"), str(item["round"])
        round_id = _stable_id("haveloc-round", company, role, round_name)
        attendance_id = _stable_id("haveloc-attendance", company, role, round_name)
        meta = item.get("meta") if isinstance(item.get("meta"), list) else []
        await db.execute(
            """INSERT INTO attendance (id, round_id, company, round_name, start_time, attendance_status, attendance_method, action_required)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
               ON CONFLICT (id) DO UPDATE SET start_time=EXCLUDED.start_time,
                   attendance_status=EXCLUDED.attendance_status, attendance_method=EXCLUDED.attendance_method,
                   action_required=EXCLUDED.action_required""",
            attendance_id, round_id, company, round_name, str(meta[0]) if meta else None,
            item.get("status") or "Unknown", str(meta[1]) if len(meta) > 1 else None, item.get("note"),
        )

    return {"jobs": jobs_persisted, "rounds": round_count, "attendance": len(attendance_items)}


async def _upsert_round(round_id: str, job_id: str | None, company: str, round_name: str,
                        round_status: object, confirmation_status: object | None,
                        scanned_at: datetime, round_date: str | None = None) -> None:
    await db.execute(
        """INSERT INTO rounds (id, job_id, company, round_name, round_date, round_status, confirmation_status, last_scanned)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
           ON CONFLICT (id) DO UPDATE SET job_id=COALESCE(EXCLUDED.job_id, rounds.job_id),
             round_date=COALESCE(EXCLUDED.round_date, rounds.round_date),
             round_status=CASE WHEN EXCLUDED.round_status='Unknown' THEN rounds.round_status ELSE EXCLUDED.round_status END,
             confirmation_status=CASE WHEN EXCLUDED.confirmation_status='Unknown' THEN rounds.confirmation_status ELSE EXCLUDED.confirmation_status END,
             last_scanned=EXCLUDED.last_scanned""",
        round_id, job_id, company, round_name, round_date, str(round_status or "Unknown"),
        str(confirmation_status or "Unknown"), scanned_at,
    )
