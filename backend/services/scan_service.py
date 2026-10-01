from datetime import datetime, timezone
import uuid
from lib import db
from models.placement import ScanResponse


async def log_activity(event_type: str, message: str, severity: str = "info", metadata: dict | None = None):
    await db.execute(
        "INSERT INTO activity_log (id, event_type, message, severity, created_at, metadata) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
        str(uuid.uuid4()), event_type, message, severity, datetime.now(timezone.utc), db.json_text(metadata or {}),
    )


async def scan_haveloc() -> ScanResponse:
    await log_activity("scan_paused", "Haveloc browser scanning is paused in the local read-only phase.", "warning", {})
    return ScanResponse(status="paused", message="Haveloc scan is paused in the local read-only phase.", last_scan_at=datetime.now(timezone.utc))
