"""Small asyncpg repository boundary for the single-user placement console."""

import json
import logging
import os
from pathlib import Path
from typing import Any

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)
pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, external_id TEXT UNIQUE NOT NULL, company TEXT NOT NULL, role TEXT NOT NULL,
  location TEXT, campus_mode TEXT, gender TEXT, batch TEXT, applicants TEXT,
  salary_ctc TEXT, stipend TEXT, apply_by TEXT, visit_date TEXT, job_type TEXT,
  application_status TEXT, haveloc_status TEXT NOT NULL, source_category TEXT NOT NULL,
  our_decision TEXT NOT NULL DEFAULT 'Review', last_scanned TIMESTAMPTZ,
  eligibility JSONB NOT NULL DEFAULT '[]', questions JSONB NOT NULL DEFAULT '[]', hiring_process JSONB NOT NULL DEFAULT '[]',
  job_description TEXT, skills JSONB NOT NULL DEFAULT '[]', attachments JSONB NOT NULL DEFAULT '[]', raw_details JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS applications (
  id TEXT PRIMARY KEY, job_id TEXT, company TEXT NOT NULL, role TEXT NOT NULL,
  applied_date TEXT, application_status TEXT NOT NULL, hiring_round_status TEXT, round_progression TEXT, last_scanned TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS rounds (
  id TEXT PRIMARY KEY, job_id TEXT, company TEXT NOT NULL, round_name TEXT NOT NULL,
  round_date TEXT, round_time TEXT, round_end_time TEXT, round_status TEXT NOT NULL, confirmation_status TEXT NOT NULL, confirmation_deadline TEXT,
  last_scanned TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS attendance (
  id TEXT PRIMARY KEY, round_id TEXT, company TEXT NOT NULL, round_name TEXT NOT NULL,
  start_time TEXT, attendance_status TEXT NOT NULL, attendance_method TEXT, action_required TEXT
);
CREATE TABLE IF NOT EXISTS activity_log (
  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, message TEXT NOT NULL, severity TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, metadata JSONB NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS scheduler_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1), scan_interval_minutes INTEGER NOT NULL DEFAULT 30,
  notifications_enabled BOOLEAN NOT NULL DEFAULT TRUE, auto_apply_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  attendance_automation_enabled BOOLEAN NOT NULL DEFAULT FALSE, email_notifications_enabled BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE IF NOT EXISTS answer_profile (
  id INTEGER PRIMARY KEY CHECK (id = 1), full_name TEXT NOT NULL DEFAULT '', github_url TEXT NOT NULL DEFAULT '',
  linkedin_url TEXT NOT NULL DEFAULT '', portfolio_url TEXT NOT NULL DEFAULT '', internship_experience TEXT NOT NULL DEFAULT '',
  preferred_role TEXT NOT NULL DEFAULT '', relocation_willingness TEXT NOT NULL DEFAULT '', physical_attendance_willingness TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gmail_messages (
  message_id TEXT PRIMARY KEY, thread_id TEXT, sender TEXT NOT NULL, subject TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL, processing_status TEXT NOT NULL, body_text TEXT NOT NULL DEFAULT '', classification_type TEXT,
  extraction_result JSONB NOT NULL DEFAULT '{}', processing_error TEXT, notification_status TEXT,
  processed_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS placement_alerts (
  id TEXT PRIMARY KEY, gmail_message_id TEXT UNIQUE NOT NULL REFERENCES gmail_messages(message_id),
  company TEXT, role TEXT, event_type TEXT NOT NULL, date_time TEXT, deadline TEXT,
  location_mode TEXT, required_action TEXT, original_subject TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL,
  notification_status TEXT NOT NULL DEFAULT 'prepared', triage_status TEXT NOT NULL DEFAULT 'new', created_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS placement_reminders (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL,
  due_at TEXT, status TEXT NOT NULL, severity TEXT NOT NULL, source_id TEXT NOT NULL,
  source_type TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
  UNIQUE (kind, source_type, source_id)
);
CREATE TABLE IF NOT EXISTS whatsapp_notifications (
  id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL,
  source_id TEXT NOT NULL, event_type TEXT NOT NULL, provider TEXT NOT NULL,
  message_body TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','failed')),
  retryable BOOLEAN NOT NULL DEFAULT FALSE, attempt_count INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ, claimed_at TIMESTAMPTZ, sent_at TIMESTAMPTZ,
  provider_message_id TEXT, error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS whatsapp_notifications_retry_idx
  ON whatsapp_notifications(status, next_attempt_at, created_at);
CREATE TABLE IF NOT EXISTS gmail_tokens (
  id INTEGER PRIMARY KEY CHECK (id = 1), access_token TEXT NOT NULL, refresh_token TEXT,
  expires_at TIMESTAMPTZ, scope TEXT
);
CREATE TABLE IF NOT EXISTS gmail_oauth_states (
  state TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS gmail_poll_state (
  id INTEGER PRIMARY KEY CHECK (id = 1), last_polled_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS auth_sessions (
  session_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_sessions_expiry_idx ON auth_sessions(expires_at);
CREATE TABLE IF NOT EXISTS auth_login_attempts (
  client_hash TEXT PRIMARY KEY, failed_attempts INTEGER NOT NULL DEFAULT 0,
  locked_until TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL
);
INSERT INTO scheduler_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
INSERT INTO answer_profile (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
INSERT INTO gmail_poll_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS location TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS campus_mode TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS gender TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS batch TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS applicants TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hiring_process JSONB NOT NULL DEFAULT '[]';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_description TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS skills JSONB NOT NULL DEFAULT '[]';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attachments JSONB NOT NULL DEFAULT '[]';
ALTER TABLE applications ADD COLUMN IF NOT EXISTS hiring_round_status TEXT;
ALTER TABLE applications ADD COLUMN IF NOT EXISTS round_progression TEXT;
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS round_end_time TEXT;
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS confirmation_deadline TEXT;
ALTER TABLE rounds ADD COLUMN IF NOT EXISTS source_gmail_message_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS rounds_source_gmail_message_id_uidx
  ON rounds(source_gmail_message_id) WHERE source_gmail_message_id IS NOT NULL;
ALTER TABLE gmail_messages ADD COLUMN IF NOT EXISTS tracking_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE attendance ADD COLUMN IF NOT EXISTS attendance_method TEXT;
ALTER TABLE placement_alerts ADD COLUMN IF NOT EXISTS triage_status TEXT NOT NULL DEFAULT 'new';
ALTER TABLE scheduler_settings ADD COLUMN IF NOT EXISTS haveloc_notification_sender TEXT NOT NULL DEFAULT '';
ALTER TABLE scheduler_settings ADD COLUMN IF NOT EXISTS gmail_polling_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE gmail_messages ADD COLUMN IF NOT EXISTS body_text TEXT NOT NULL DEFAULT '';
UPDATE scheduler_settings SET auto_apply_enabled=FALSE, attendance_automation_enabled=FALSE, email_notifications_enabled=FALSE WHERE id=1;
"""

JSON_COLUMNS = frozenset({
    "eligibility",
    "questions",
    "hiring_process",
    "skills",
    "attachments",
    "raw_details",
    "metadata",
    "extraction_result",
})


async def connect_db() -> bool:
    global pool
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        logger.warning("DATABASE_URL is not configured; dashboard will run in empty read-only mode")
        return False
    candidate_pool: asyncpg.Pool | None = None
    try:
        candidate_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=5, command_timeout=15)
        await candidate_pool.execute(SCHEMA)
        pool = candidate_pool
        return True
    except Exception as exc:
        logger.error("PostgreSQL unavailable (%s)", type(exc).__name__)
        pool = None
        if candidate_pool is not None:
            await candidate_pool.close()
        return False


async def close_db() -> None:
    global pool
    if pool:
        await pool.close()
        pool = None


async def fetch_all(query: str, *args: Any) -> list[dict[str, Any]]:
    if not pool:
        return []
    async with pool.acquire() as connection:
        return [_decode_json_columns(dict(row)) for row in await connection.fetch(query, *args)]


async def fetch_one(query: str, *args: Any) -> dict[str, Any] | None:
    rows = await fetch_all(query, *args)
    return rows[0] if rows else None


async def execute(query: str, *args: Any) -> bool:
    if not pool:
        return False
    async with pool.acquire() as connection:
        await connection.execute(query, *args)
    return True


def json_text(value: Any) -> str:
    return json.dumps(value, default=str)


def _decode_json_columns(row: dict[str, Any]) -> dict[str, Any]:
    """Decode asyncpg's default JSON/JSONB strings for API models and callers."""

    for column in JSON_COLUMNS.intersection(row):
        value = row[column]
        if isinstance(value, str):
            try:
                row[column] = json.loads(value)
            except json.JSONDecodeError:
                # Leave malformed legacy data visible as invalid instead of hiding it.
                continue
    return row


def database_configured() -> bool:
    return bool(os.environ.get("DATABASE_URL", "").strip())
