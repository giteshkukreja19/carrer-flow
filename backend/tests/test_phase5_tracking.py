import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from lib import db, reminders
from models.placement import AlertTriageUpdate
from routers import placement
from services import tracking
from services.llm_classifier import EmailExtraction


RECEIVED = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def extraction(event_type, **updates):
    values = {
        "company": "Acme",
        "role": "Platform Intern",
        "event_type": event_type,
        "deadline": None,
        "round": None,
        "date": None,
        "time": None,
        "location_mode": None,
        "required_action": None,
        "application_status": None,
        "original_subject": "Acme placement update",
        "timestamp": RECEIVED.isoformat(),
    }
    values.update(updates)
    return EmailExtraction(**values)


class TrackingStore:
    def __init__(self):
        self.jobs = {}
        self.rounds = {}
        self.applications = {
            "app-1": {
                "id": "app-1", "company": "Acme", "role": "Platform Intern",
                "application_status": "Applied", "last_scanned": None,
            }
        }
        self.attendance = []

    async def fetch_all(self, query, *args):
        if "FROM rounds WHERE company=$1 AND round_name=$2" in query:
            return [
                {"id": row["id"], "round_date": row["round_date"], "round_time": row["round_time"]}
                for row in self.rounds.values()
                if row["company"] == args[0] and row["round_name"] == args[1]
            ][:2]
        if "FROM applications WHERE company=$1 AND role=$2" in query:
            return [
                {"id": row["id"]}
                for row in self.applications.values()
                if row["company"] == args[0] and row["role"] == args[1]
            ][:2]
        raise AssertionError(f"Unexpected query: {query}")

    async def fetch_one(self, query, *args):
        if "INSERT INTO jobs" in query:
            record_id, external_id, company, role, apply_by, scanned, raw_details = args
            if external_id in self.jobs:
                return None
            self.jobs[external_id] = {
                "id": record_id, "external_id": external_id, "company": company,
                "role": role, "apply_by": apply_by, "last_scanned": scanned,
                "raw_details": raw_details,
            }
            return {"id": record_id}
        if "INSERT INTO rounds" in query:
            record_id, company, name, date, time, scanned, source_id = args
            current = self.rounds.get(record_id)
            if current:
                changed = current["round_date"] != date or current["round_time"] != time
                if not changed:
                    return None
                current.update(round_date=date, round_time=time, last_scanned=scanned)
                return {"id": record_id}
            self.rounds[record_id] = {
                "id": record_id, "company": company, "round_name": name,
                "round_date": date, "round_time": time, "round_status": "Scheduled",
                "confirmation_status": "Unknown", "source_gmail_message_id": source_id,
                "last_scanned": scanned,
            }
            return {"id": record_id}
        if query.lstrip().startswith("UPDATE rounds SET"):
            record_id, date, time, scanned = args
            current = next(row for row in self.rounds.values() if row["id"] == record_id)
            if date is not None:
                current["round_date"] = date
            if time is not None:
                current["round_time"] = time
            current["last_scanned"] = scanned
            return {"id": record_id}
        if query.lstrip().startswith("UPDATE applications SET"):
            record_id, status, scanned = args
            self.applications[record_id]["application_status"] = status
            self.applications[record_id]["last_scanned"] = scanned
            return {"id": record_id}
        raise AssertionError(f"Unexpected query: {query}")


def patch_tracking_store(monkeypatch, store):
    monkeypatch.setattr(tracking.db, "fetch_one", store.fetch_one)
    monkeypatch.setattr(tracking.db, "fetch_all", store.fetch_all)


def test_job_materialization_is_source_linked_and_idempotent(monkeypatch):
    store = TrackingStore()
    patch_tracking_store(monkeypatch, store)
    event = extraction("NEW_JOB", deadline="15 Oct 2026")

    first = run(tracking.materialize_placement_event("gmail-001", event))
    second = run(tracking.materialize_placement_event("gmail-001", event))

    assert len(store.jobs) == 1
    row = next(iter(store.jobs.values()))
    assert row["company"] == "Acme"
    assert row["role"] == "Platform Intern"
    assert row["apply_by"] == "15 Oct 2026"
    assert row["external_id"] == "gmail:gmail-001"
    assert len(first) == 1
    assert second == []


def test_deadline_materializes_only_stated_deadline(monkeypatch):
    store = TrackingStore()
    patch_tracking_store(monkeypatch, store)

    run(tracking.materialize_placement_event(
        "gmail-deadline",
        extraction("APPLICATION_DEADLINE", deadline="15 Oct 2026"),
    ))
    row = next(iter(store.jobs.values()))

    assert row["apply_by"] == "15 Oct 2026"


def test_round_creation_and_changed_round_update_are_idempotent(monkeypatch):
    store = TrackingStore()
    patch_tracking_store(monkeypatch, store)
    scheduled = extraction(
        "ROUND_SCHEDULED", round="Technical interview", date="12 Oct 2026", time="10:30 AM"
    )

    created = run(tracking.materialize_placement_event("gmail-round", scheduled))
    repeated = run(tracking.materialize_placement_event("gmail-round", scheduled))
    changed = run(tracking.materialize_placement_event(
        "gmail-change",
        extraction("ROUND_CHANGED", round="Technical interview", date="13 Oct 2026"),
    ))

    assert len(store.rounds) == 1
    row = next(iter(store.rounds.values()))
    assert row["round_date"] == "13 Oct 2026"
    assert row["round_time"] == "10:30 AM"
    assert row["source_gmail_message_id"] == "gmail-round"
    assert [event["event_type"] for event in created] == ["round_updated"]
    assert repeated == []
    assert [event["event_type"] for event in changed] == ["round_updated"]


def test_round_change_skips_ambiguous_company_and_round(monkeypatch):
    store = TrackingStore()
    store.rounds["round-1"] = {"id": "round-1", "company": "Acme", "round_name": "Interview", "round_date": "12 Oct 2026", "round_time": None}
    store.rounds["round-2"] = {"id": "round-2", "company": "Acme", "round_name": "Interview", "round_date": "13 Oct 2026", "round_time": None}
    patch_tracking_store(monkeypatch, store)

    events = run(tracking.materialize_placement_event(
        "gmail-change",
        extraction("ROUND_CHANGED", round="Interview", date="14 Oct 2026"),
    ))

    assert events == []
    assert store.rounds["round-1"]["round_date"] == "12 Oct 2026"
    assert store.rounds["round-2"]["round_date"] == "13 Oct 2026"


def test_application_status_updates_only_matching_existing_application(monkeypatch):
    store = TrackingStore()
    patch_tracking_store(monkeypatch, store)

    events = run(tracking.materialize_placement_event(
        "gmail-status",
        extraction("APPLICATION_STATUS", application_status="Shortlisted"),
    ))

    assert store.applications["app-1"]["application_status"] == "Shortlisted"
    assert events[0]["event_type"] == "application_status_updated"


def test_attendance_required_does_not_create_or_mark_attendance(monkeypatch):
    store = TrackingStore()
    patch_tracking_store(monkeypatch, store)

    events = run(tracking.materialize_placement_event(
        "gmail-attendance",
        extraction("ATTENDANCE_REQUIRED", required_action="Confirm attendance by 5 PM"),
    ))

    assert events == []
    assert store.attendance == []


def test_alert_fields_and_missing_optional_values_are_preserved():
    from services.ingestion import _build_alert

    alert = _build_alert(
        "gmail-1",
        RECEIVED,
        extraction(
            "ROUND_SCHEDULED",
            round="Technical interview",
            date="12 Oct 2026",
            time="10:30 AM",
            location_mode="Online",
            required_action="Join the meeting",
        ),
    )

    assert alert.round == "Technical interview"
    assert alert.date == "12 Oct 2026"
    assert alert.time == "10:30 AM"
    assert alert.location_mode == "Online"
    missing = _build_alert("gmail-2", RECEIVED, extraction("OTHER", company=None, role=None))
    assert missing.company is None
    assert missing.date is None
    assert missing.time is None
    assert missing.required_action is None


def test_reminder_candidates_are_stable_and_cover_deadline_round_attendance():
    job_rows = [{
        "id": "job-1", "external_id": "gmail:msg-deadline", "source_category": "gmail",
        "company": "Acme", "role": "Platform Intern", "apply_by": "2026-10-02T10:00:00+00:00",
    }]
    round_rows = [{
        "id": "round-1", "source_gmail_message_id": "msg-round", "company": "Acme",
        "round_name": "Interview", "round_date": "2026-10-02T10:00:00+00:00", "round_time": "10:00 AM",
    }]
    attendance_rows = [{
        "id": "attendance-1", "company": "Acme", "round_name": "Interview",
        "start_time": None, "action_required": "Bring student ID",
    }]
    alerts = [{
        "gmail_message_id": "msg-attendance", "company": "Acme", "role": "Platform Intern",
        "event_type": "ATTENDANCE_REQUIRED", "date_time": None,
        "required_action": "Bring student ID", "round": "Interview",
    }]

    first = reminders.build_reminders(job_rows, round_rows, attendance_rows, alerts, now=NOW)
    second = reminders.build_reminders(job_rows, round_rows, attendance_rows, alerts, now=NOW)

    assert [(row.kind, row.source_type, row.source_id) for row in first] == [
        (row.kind, row.source_type, row.source_id) for row in second
    ]
    assert {row.kind for row in first} == {"application_deadline", "round", "attendance"}
    assert len([row for row in first if row.kind == "attendance"]) == 2


class ReminderStore:
    def __init__(self):
        self.rows = {}
        self.activities = []

    async def fetch_one(self, query, *args):
        if query.startswith("SELECT id FROM placement_reminders"):
            key = (args[0], args[1], args[2])
            row = self.rows.get(key)
            return {"id": row["id"]} if row else None
        if "INSERT INTO placement_reminders" in query:
            rid, kind, title, description, due, status, severity, source_id, source_type, created = args
            key = (kind, source_type, source_id)
            if key in self.rows:
                old = self.rows[key]
                if (old["title"], old["description"], old["due_at"], old["severity"]) == (title, description, due, severity):
                    return None
                old.update(title=title, description=description, due_at=due, severity=severity)
                return dict(old)
            self.rows[key] = {
                "id": rid, "kind": kind, "title": title, "description": description,
                "due_at": due, "status": status, "severity": severity,
                "source_id": source_id, "source_type": source_type, "created_at": created,
            }
            return dict(self.rows[key])
        raise AssertionError(query)

    async def fetch_all(self, query, *args):
        if "FROM placement_reminders" in query:
            return list(self.rows.values())
        return []

    async def execute(self, query, *args):
        self.activities.append((query, args))
        return True


def test_reminder_persistence_is_idempotent_and_audited(monkeypatch):
    store = ReminderStore()
    monkeypatch.setattr(db, "pool", object())
    monkeypatch.setattr(db, "fetch_one", store.fetch_one)
    monkeypatch.setattr(db, "fetch_all", store.fetch_all)
    monkeypatch.setattr(db, "execute", store.execute)
    candidates = reminders.build_reminders(
        [{"id": "j1", "company": "Acme", "role": "Intern", "apply_by": "2026-10-02T10:00:00+00:00"}],
        [],
        [],
        now=NOW,
    )

    first = run(reminders.persist_reminders(candidates))
    second = run(reminders.persist_reminders(candidates))

    assert len(store.rows) == 1
    assert len(first) == len(second) == 1
    assert sum("INSERT INTO activity_log" in query for query, _ in store.activities) == 1


def test_dashboard_returns_alerts_and_survives_optional_fields_missing(monkeypatch):
    alert_row = {
        "id": "alert-1", "gmail_message_id": "message-1", "company": "Acme", "role": "Intern",
        "event_type": "ROUND_SCHEDULED", "date_time": "12 Oct 2026 10:00 AM",
        "deadline": None, "location_mode": None, "required_action": None,
        "original_subject": "Interview invite", "received_at": RECEIVED, "created_at": RECEIVED,
        "notification_status": "prepared", "triage_status": "new",
        "source_sender": "placement@example.test",
        "extraction_result": {"round": "Technical", "date": "12 Oct 2026", "time": "10:00 AM"},
    }

    async def fetch_all(query, *args):
        if "FROM placement_alerts a" in query:
            return [alert_row]
        return []

    async def fetch_one(query, *args):
        return None

    async def integration_status(*args):
        return {
            "oauth_configured": False, "gmail_oauth_configured": False, "gmail_connected": False,
            "gemini_configured": False, "sender_configured": False, "polling_enabled": False,
            "last_polled_at": None,
        }

    monkeypatch.setattr(placement.db, "fetch_all", fetch_all)
    monkeypatch.setattr(placement.db, "fetch_one", fetch_one)
    monkeypatch.setattr(placement.db, "pool", None)
    monkeypatch.setattr(placement.db, "database_configured", lambda: False)
    monkeypatch.setattr(placement, "integration_status", integration_status)

    response = run(placement.dashboard())
    data = response.model_dump(mode="json")

    assert data["alerts"][0]["round"] == "Technical"
    assert data["alerts"][0]["source_message_id"] == "message-1"
    assert data["alerts"][0]["triage_status"] == "new"
    assert data["jobs"] == []
    assert data["reminders"] == []


def test_alert_triage_rules_and_activity(monkeypatch):
    row = {
        "id": "alert-1", "gmail_message_id": "message-1", "company": "Acme", "role": "Intern",
        "event_type": "NEW_JOB", "date_time": None, "deadline": None, "location_mode": None,
        "required_action": None, "original_subject": "New job", "received_at": RECEIVED,
        "created_at": RECEIVED, "notification_status": "prepared", "triage_status": "new",
        "source_sender": None, "extraction_result": {},
    }
    state = {"value": "new"}
    activities = []

    async def fetch_one(query, *args):
        if query.lstrip().startswith("SELECT a.*"):
            if args[0] != "alert-1":
                return None
            row["triage_status"] = state["value"]
            return dict(row)
        if query.lstrip().startswith("UPDATE placement_alerts"):
            _, requested, current = args
            if state["value"] != current:
                return None
            state["value"] = requested
            return {"id": "alert-1"}
        raise AssertionError(query)

    async def log_activity(event_type, message, severity, metadata):
        activities.append((event_type, message, severity, metadata))

    monkeypatch.setattr(placement.db, "fetch_one", fetch_one)
    monkeypatch.setattr(placement, "log_activity", log_activity)

    reviewed = run(placement.update_alert_triage("alert-1", AlertTriageUpdate(status="review")))
    assert reviewed.triage_status == "review"
    assert activities[0][0] == "alert_reviewed"

    with pytest.raises(HTTPException) as invalid:
        run(placement.update_alert_triage("alert-1", AlertTriageUpdate(status="review")))
    assert invalid.value.status_code == 409

    acknowledged = run(placement.update_alert_triage("alert-1", AlertTriageUpdate(status="acknowledged")))
    assert acknowledged.triage_status == "acknowledged"
    assert activities[-1][0] == "alert_acknowledged"

    with pytest.raises(HTTPException) as missing:
        run(placement.update_alert_triage("not-found", AlertTriageUpdate(status="review")))
    assert missing.value.status_code == 404


def test_triage_does_not_allow_new_directly_to_acknowledged(monkeypatch):
    async def fetch_one(query, *args):
        return {
            "id": "alert-1", "gmail_message_id": "message-1", "event_type": "NEW_JOB",
            "original_subject": "new", "received_at": RECEIVED, "triage_status": "new",
        }

    monkeypatch.setattr(placement.db, "fetch_one", fetch_one)

    with pytest.raises(HTTPException) as invalid:
        run(placement.update_alert_triage("alert-1", AlertTriageUpdate(status="acknowledged")))
    assert invalid.value.status_code == 409


def test_ambiguous_application_status_does_not_update_any_application(monkeypatch):
    store = TrackingStore()
    store.applications["app-2"] = {
        "id": "app-2", "company": "Acme", "role": "Platform Intern",
        "application_status": "Applied", "last_scanned": None,
    }
    patch_tracking_store(monkeypatch, store)

    events = run(tracking.materialize_placement_event(
        "gmail-status",
        extraction("APPLICATION_STATUS", application_status="Shortlisted"),
    ))

    assert events == []
    assert store.applications["app-1"]["application_status"] == "Applied"
    assert store.applications["app-2"]["application_status"] == "Applied"
