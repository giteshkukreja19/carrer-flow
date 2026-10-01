import asyncio
import json
from datetime import datetime, timezone

import pytest

from lib import db
from models.placement import PlacementAlert
from services import ingestion
from services.llm_classifier import EmailExtraction

RECEIVED_AT = datetime(2026, 9, 30, 9, 15, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def test_database_decodes_jsonb_for_dashboard_models():
    row = db._decode_json_columns(
        {
            "metadata": '{"gmail_message_id":"message-1"}',
            "raw_details": '{"source":"gmail"}',
            "eligibility": '["degree stated"]',
            "subject": '{"kept":"as text"}',
        }
    )

    assert row["metadata"] == {"gmail_message_id": "message-1"}
    assert row["raw_details"] == {"source": "gmail"}
    assert row["eligibility"] == ["degree stated"]
    assert row["subject"] == '{"kept":"as text"}'


def extraction(**updates):
    values = {
        "company": "Acme",
        "role": "Backend Intern",
        "event_type": "NEW_JOB",
        "deadline": "30 Sep 2026",
        "round": None,
        "date": None,
        "time": None,
        "location_mode": None,
        "required_action": None,
        "original_subject": "Acme Backend Intern opening",
        "timestamp": RECEIVED_AT.isoformat(),
    }
    values.update(updates)
    return EmailExtraction(**values)


class FakeStore:
    def __init__(self, messages):
        self.messages = {message["message_id"]: dict(message) for message in messages}
        self.alerts = {}
        self.activities = []
        self.extractions = {}

    async def fetch_all(self, query, *args):
        if "FROM gmail_messages" not in query:
            return []
        retryable = set(args[0])
        limit = args[1]
        return [
            dict(message)
            for message in self.messages.values()
            if message["processing_status"] in retryable
            or (message["processing_status"] == "classified" and message.get("tracking_status") == "pending")
        ][:limit]

    async def fetch_one(self, query, *args):
        if "WITH pending_message AS" not in query:
            raise AssertionError(f"Unexpected query: {query}")
        message_id, event_type, extraction_json = args[:3]
        message = self.messages[message_id]
        if message["processing_status"] not in set(args[4]) or message_id in self.alerts:
            return None

        extraction_result = json.loads(extraction_json)
        self.extractions[message_id] = extraction_result
        message["processing_status"] = "classified"
        message["tracking_status"] = "pending"
        message["classification_type"] = event_type
        message["extraction_result"] = extraction_result
        message["processing_error"] = None
        alert = {
            "id": args[5],
            "gmail_message_id": message_id,
            "company": args[6],
            "role": args[7],
            "event_type": event_type,
            "date_time": args[8],
            "deadline": args[9],
            "location_mode": args[10],
            "required_action": args[11],
            "original_subject": args[12],
            "received_at": message["received_at"],
            "notification_status": "prepared",
            "triage_status": "new",
            "created_at": args[13],
        }
        self.alerts[message_id] = alert
        return {"gmail_message_id": message_id}

    async def execute(self, query, *args):
        if "tracking_status='complete'" in query:
            self.messages[args[0]]["tracking_status"] = "complete"
            return True
        if "UPDATE gmail_messages" in query:
            message_id, statuses = args
            message = self.messages[message_id]
            if message["processing_status"] in set(statuses):
                message["processing_status"] = "pending_classification"
                message["processing_error"] = "Classification failed; message is safe to retry."
            return True
        return True


def pending_message(message_id="message-1"):
    return {
        "message_id": message_id,
        "sender": "placement@example.test",
        "subject": "Acme Backend Intern opening",
        "received_at": RECEIVED_AT,
        "body_text": "Acme is hiring a Backend Intern. Apply by 30 Sep 2026.",
        "processing_status": "pending_classification",
        "processing_error": None,
    }


def patch_store(monkeypatch, store):
    monkeypatch.setattr(ingestion.db, "fetch_all", store.fetch_all)
    monkeypatch.setattr(ingestion.db, "fetch_one", store.fetch_one)
    monkeypatch.setattr(ingestion.db, "execute", store.execute)
    monkeypatch.setattr(ingestion, "log_activity", _capture_activity(store))
    monkeypatch.setattr(ingestion, "materialize_placement_event", _no_tracking_events)
    monkeypatch.setattr(ingestion, "gemini_configured", lambda: True)


def _capture_activity(store):
    async def log_activity(event_type, message, severity, metadata):
        store.activities.append((event_type, message, severity, metadata))

    return log_activity


def test_success_persists_extraction_and_dashboard_compatible_alert(monkeypatch):
    store = FakeStore([pending_message()])
    patch_store(monkeypatch, store)
    monkeypatch.setattr(ingestion, "classify_email", _async_value(extraction(round="First round")))

    classified_count = run(ingestion.process_pending_gmail_messages())

    assert classified_count == 1
    assert store.messages["message-1"]["processing_status"] == "classified"
    assert store.messages["message-1"]["classification_type"] == "NEW_JOB"
    assert store.extractions["message-1"]["round"] == "First round"
    assert store.extractions["message-1"]["timestamp"] == RECEIVED_AT.isoformat()
    assert len(store.alerts) == 1
    alert = PlacementAlert(**store.alerts["message-1"])
    assert alert.gmail_message_id == "message-1"
    assert alert.deadline == "30 Sep 2026"
    assert alert.date_time is None
    assert any(event[0] == "email_classified" for event in store.activities)


def test_duplicate_message_cannot_create_a_second_alert(monkeypatch):
    store = FakeStore([pending_message()])
    patch_store(monkeypatch, store)
    result = extraction()

    assert run(ingestion._persist_classification(store.messages["message-1"], result)) is True
    assert run(ingestion._persist_classification(store.messages["message-1"], result)) is False
    assert len(store.alerts) == 1


def test_classification_failure_remains_pending_and_succeeds_on_retry(monkeypatch):
    store = FakeStore([pending_message()])
    patch_store(monkeypatch, store)
    calls = {"count": 0}

    async def flaky_classifier(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("provider details must not be exposed")
        return extraction()

    monkeypatch.setattr(ingestion, "classify_email", flaky_classifier)

    assert run(ingestion.process_pending_gmail_messages()) == 0
    assert store.messages["message-1"]["processing_status"] == "pending_classification"
    assert store.messages["message-1"]["processing_error"]
    assert not store.alerts

    assert run(ingestion.process_pending_gmail_messages()) == 1
    assert store.messages["message-1"]["processing_status"] == "classified"
    assert len(store.alerts) == 1
    assert any(event[0] == "email_classification_failed" for event in store.activities)


def test_unavailable_classifier_does_not_read_or_mutate_pending_messages(monkeypatch):
    store = FakeStore([pending_message()])
    monkeypatch.setattr(ingestion, "gemini_configured", lambda: False)

    assert run(ingestion.process_pending_gmail_messages()) == 0
    assert store.messages["message-1"]["processing_status"] == "pending_classification"
    assert not store.alerts


def test_poll_stores_raw_message_pending_even_without_ai_provider(monkeypatch):
    store = FakeStore([])
    stored = {}
    db_executions = []

    async def fetch_one(query, *args):
        if "FROM scheduler_settings" in query:
            return {"haveloc_notification_sender": "placement@example.test", "gmail_polling_enabled": True}
        if "FROM gmail_poll_state" in query:
            return {"last_polled_at": None}
        if "INSERT INTO gmail_messages" in query:
            stored.update(
                {
                    "message_id": args[0],
                    "sender": args[2],
                    "subject": args[3],
                    "received_at": args[4],
                    "processing_status": "pending_classification",
                    "body_text": args[5],
                }
            )
            return {"message_id": args[0]}
        raise AssertionError(f"Unexpected query: {query}")

    async def execute(query, *args):
        db_executions.append((query, args))
        return True

    async def activities(event_type, message, severity, metadata):
        store.activities.append((event_type, message, severity, metadata))

    async def process_pending():
        return 0

    async def fetch_messages(sender, since):
        from services.gmail import GmailMessage

        return [
            GmailMessage(
                message_id="new-message",
                thread_id="thread-1",
                sender=sender,
                subject="Acme placement notice",
                received_at=RECEIVED_AT,
                body="Acme role notice source content",
            )
        ]

    async def integration_status(sender, enabled, last_polled_at):
        return {"gmail_oauth_configured": True, "gmail_connected": True}

    monkeypatch.setattr(ingestion.db, "fetch_one", fetch_one)
    monkeypatch.setattr(ingestion.db, "execute", execute)
    monkeypatch.setattr(ingestion, "oauth_configured", lambda: True)
    monkeypatch.setattr(ingestion, "integration_status", integration_status)
    monkeypatch.setattr(ingestion, "fetch_messages", fetch_messages)
    monkeypatch.setattr(ingestion, "process_pending_gmail_messages", process_pending)
    monkeypatch.setattr(ingestion, "log_activity", activities)

    assert run(ingestion.poll_gmail_once()) == "ingested:1"
    assert stored["processing_status"] == "pending_classification"
    assert stored["body_text"] == "Acme role notice source content"
    assert len(db_executions) == 1


def test_dashboard_response_reads_a_persisted_alert(monkeypatch):
    from routers import placement

    stored_alert = {
        "id": "alert-1",
        "gmail_message_id": "message-1",
        "company": "Acme",
        "role": "Backend Intern",
        "event_type": "NEW_JOB",
        "date_time": None,
        "deadline": "30 Sep 2026",
        "location_mode": None,
        "required_action": None,
        "original_subject": "Acme Backend Intern opening",
        "received_at": RECEIVED_AT,
        "notification_status": "prepared",
        "triage_status": "new",
    }

    async def fetch_all(query, *args):
        if "FROM placement_alerts" in query:
            return [stored_alert]
        return []

    async def fetch_one(query, *args):
        return None

    async def status(sender, enabled, last_polled_at):
        return {
            "oauth_configured": False,
            "gmail_oauth_configured": False,
            "gmail_connected": False,
            "gemini_configured": False,
            "sender_configured": False,
            "polling_enabled": False,
            "last_polled_at": None,
        }

    monkeypatch.setattr(placement.db, "fetch_all", fetch_all)
    monkeypatch.setattr(placement.db, "fetch_one", fetch_one)
    # Imported function lookup uses the router module binding.
    monkeypatch.setattr(placement, "integration_status", status)

    response = run(placement.dashboard())
    assert response.alerts[0].gmail_message_id == "message-1"
    assert response.alerts[0].deadline == "30 Sep 2026"


def _async_value(value):
    async def return_value(**kwargs):
        return value

    return return_value


async def _no_tracking_events(*args, **kwargs):
    return []


def test_materialization_failure_retries_saved_extraction_without_reclassifying(monkeypatch):
    result = extraction()
    message = pending_message()
    message["processing_status"] = "classified"
    message["tracking_status"] = "pending"
    message["extraction_result"] = result.model_dump(mode="json")
    store = FakeStore([message])
    patch_store(monkeypatch, store)
    monkeypatch.setattr(ingestion, "gemini_configured", lambda: False)
    attempts = {"count": 0}

    async def flaky_materializer(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("internal database detail")
        return []

    monkeypatch.setattr(ingestion, "materialize_placement_event", flaky_materializer)

    assert run(ingestion.process_pending_gmail_messages()) == 0
    assert store.messages["message-1"]["tracking_status"] == "pending"
    assert store.messages["message-1"]["processing_status"] == "classified"

    assert run(ingestion.process_pending_gmail_messages()) == 0
    assert store.messages["message-1"]["tracking_status"] == "complete"
    assert attempts["count"] == 2
    assert any(event[0] == "placement_tracking_failed" for event in store.activities)
