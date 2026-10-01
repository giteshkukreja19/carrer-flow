import asyncio
import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest

from lib import db
from models.placement import PlacementAlert
from routers import placement
from services import notifications as service


NOW = datetime(2026, 10, 1, 10, tzinfo=timezone.utc)
FAKE_SECRET = "not-a-real-whatsapp-token"


def run(coro):
    return asyncio.run(coro)


def configure_whatsapp(monkeypatch):
    monkeypatch.setenv("WHATSAPP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_PROVIDER", "meta_cloud")
    monkeypatch.setenv("WHATSAPP_API_VERSION", "v99.0")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789012345")
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", FAKE_SECRET)
    monkeypatch.setenv("WHATSAPP_RECIPIENT_NUMBER", "+919876543210")


def alert(event_type="NEW_JOB", **updates):
    values = {
        "id": "alert-1",
        "gmail_message_id": "gmail-1",
        "company": "Acme",
        "role": "Platform Intern",
        "event_type": event_type,
        "round": None,
        "date": None,
        "time": None,
        "date_time": None,
        "deadline": None,
        "location_mode": None,
        "required_action": None,
        "application_status": None,
        "original_subject": "Source subject",
        "received_at": NOW,
    }
    values.update(updates)
    return PlacementAlert(**values)


@pytest.mark.parametrize(
    "event_type",
    [
        "NEW_JOB",
        "APPLICATION_DEADLINE",
        "ROUND_SCHEDULED",
        "ROUND_CHANGED",
        "ROUND_CONFIRMATION",
        "ATTENDANCE_REQUIRED",
        "APPLICATION_STATUS",
    ],
)
def test_message_formatting_supports_each_actionable_event(event_type):
    result = service.format_alert_message(alert(event_type))

    assert result.startswith("PLACEMENT ALERT")
    assert "Company: Acme" in result
    assert "Role: Platform Intern" in result
    assert f"Event: {event_type.replace('_', ' ')}" in result
    assert "Source subject" not in result


def test_message_formatter_omits_missing_fields_and_ignores_other_events():
    result = service.format_alert_message(
        alert(
            "ROUND_SCHEDULED",
            company=None,
            role=None,
            date=None,
            time=None,
            deadline=None,
            round=None,
            location_mode=None,
            required_action=None,
        )
    )
    assert result == "\n".join(("PLACEMENT ALERT", "Event: ROUND SCHEDULED"))
    assert service.format_alert_message(alert("OTHER")) is None


def test_application_status_message_includes_only_explicit_status():
    result = service.format_alert_message(
        alert("APPLICATION_STATUS", application_status="Shortlisted")
    )
    assert "Status: Shortlisted" in result


def test_whatsapp_configuration_requires_explicit_enable_and_all_fields(monkeypatch):
    for key in (
        "WHATSAPP_ENABLED",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_API_VERSION",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_RECIPIENT_NUMBER",
    ):
        monkeypatch.delenv(key, raising=False)

    assert service.whatsapp_configured() is False
    assert service.whatsapp_enabled() is False
    assert service.notification_status()["configured"] is False

    configure_whatsapp(monkeypatch)
    assert service.whatsapp_configured() is True
    assert service.whatsapp_enabled() is True

    monkeypatch.setenv("WHATSAPP_ENABLED", "")
    assert service.whatsapp_configured() is True
    assert service.whatsapp_enabled() is False


def test_global_notifications_kill_switch_blocks_whatsapp(monkeypatch):
    configure_whatsapp(monkeypatch)
    monkeypatch.setenv("NOTIFICATIONS_ENABLED", "false")
    assert service.whatsapp_configured() is True
    assert service.whatsapp_enabled() is False
    assert run(service.run_notification_cycle(now=NOW))["status"] == "disabled"


def test_meta_provider_builds_expected_request_without_network(monkeypatch):
    captured = {}

    class FakeResponse(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.get_header("Authorization")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse(b'{"messages":[{"id":"wamid.test"}]}')

    monkeypatch.setattr(service, "urlopen", fake_urlopen)
    provider = service.MetaWhatsAppCloudProvider(
        phone_number_id="123456789012345",
        recipient_number="919876543210",
        api_version="v99.0",
        access_token=FAKE_SECRET,
    )

    result = run(provider.send_message("PLACEMENT ALERT", idempotency_key="alert:test"))

    assert result == "wamid.test"
    assert captured["url"].endswith("/v99.0/123456789012345/messages")
    assert captured["authorization"] == f"Bearer {FAKE_SECRET}"
    assert captured["payload"]["to"] == "919876543210"
    assert captured["payload"]["text"]["body"] == "PLACEMENT ALERT"
    assert captured["timeout"] == 20


class FakeStore:
    def __init__(self):
        self.deliveries = {}
        self.reminders = []
        self.alerts = []
        self.activities = []

    async def fetch_one(self, query, *args):
        if "INSERT INTO whatsapp_notifications" in query:
            delivery_id, source_key, source_type, source_id, event_type, body, created = args
            if source_key in self.deliveries:
                return None
            row = {
                "id": delivery_id,
                "source_key": source_key,
                "source_type": source_type,
                "source_id": source_id,
                "event_type": event_type,
                "provider": "meta_cloud",
                "message_body": body,
                "status": "pending",
                "retryable": False,
                "attempt_count": 0,
                "next_attempt_at": None,
                "claimed_at": None,
                "sent_at": None,
                "provider_message_id": None,
                "error_code": None,
                "created_at": created,
                "updated_at": created,
            }
            self.deliveries[source_key] = row
            return dict(row)
        if "FROM whatsapp_notifications WHERE source_key=$1" in query:
            row = self.deliveries.get(args[0])
            return dict(row) if row else None
        if "FROM whatsapp_notifications WHERE id=$1" in query:
            row = next((item for item in self.deliveries.values() if item["id"] == args[0]), None)
            return dict(row) if row else None
        if query.lstrip().startswith("UPDATE whatsapp_notifications") and "RETURNING *" in query:
            delivery_id, now = args
            row = next((item for item in self.deliveries.values() if item["id"] == delivery_id), None)
            if not row:
                return None
            is_due = (
                row["status"] == "pending"
                and (row["next_attempt_at"] is None or row["next_attempt_at"] <= now)
            ) or (
                row["status"] == "failed"
                and row["retryable"]
                and row["next_attempt_at"] is not None
                and row["next_attempt_at"] <= now
            )
            if not is_due:
                return None
            row["status"] = "sending"
            row["retryable"] = False
            row["attempt_count"] += 1
            row["claimed_at"] = now
            row["updated_at"] = now
            row["next_attempt_at"] = None
            return dict(row)
        if "SELECT status FROM whatsapp_notifications WHERE id=$1" in query:
            row = next((item for item in self.deliveries.values() if item["id"] == args[0]), None)
            return {"status": row["status"]} if row else None
        raise AssertionError(f"Unexpected fetch_one: {query}")

    async def fetch_all(self, query, *args):
        if query.lstrip().startswith("UPDATE whatsapp_notifications"):
            return []
        if "FROM placement_alerts" in query:
            return list(self.alerts)
        if "FROM placement_reminders" in query:
            return list(self.reminders)
        if "FROM whatsapp_notifications" in query:
            if not args:
                return [dict(row) for row in self.deliveries.values() if row["status"] == "pending" or row["retryable"]]
            now = args[0] if args else NOW
            eligible = [
                row for row in self.deliveries.values()
                if row["status"] == "pending"
                or (
                    row["status"] == "failed"
                    and row["retryable"]
                    and row["next_attempt_at"] is not None
                    and row["next_attempt_at"] <= now
                )
            ]
            return [{"id": row["id"]} for row in eligible[: args[1] if len(args) > 1 else 100]]
        raise AssertionError(f"Unexpected fetch_all: {query}")

    async def execute(self, query, *args):
        if "SET status='failed'" in query:
            delivery_id, retryable, retry_at, error_code, updated = args
            row = next(row for row in self.deliveries.values() if row["id"] == delivery_id)
            row.update(
                status="failed",
                retryable=retryable,
                next_attempt_at=retry_at,
                claimed_at=None,
                error_code=error_code,
                updated_at=updated,
            )
            return True
        if "SET status='sent'" in query:
            delivery_id, sent_at, provider_message_id = args
            row = next(row for row in self.deliveries.values() if row["id"] == delivery_id)
            row.update(
                status="sent",
                retryable=False,
                sent_at=sent_at,
                updated_at=sent_at,
                claimed_at=None,
                next_attempt_at=None,
                error_code=None,
                provider_message_id=provider_message_id,
            )
            return True
        self.activities.append((query, args))
        return True


def patch_store(monkeypatch, store):
    monkeypatch.setattr(db, "fetch_one", store.fetch_one)
    monkeypatch.setattr(db, "fetch_all", store.fetch_all)
    monkeypatch.setattr(db, "execute", store.execute)

    async def capture_activity(event_type, message, metadata):
        store.activities.append((event_type, message, metadata))

    monkeypatch.setattr(service, "_safe_activity", capture_activity)


class FakeProvider:
    def __init__(self, result="wamid.fake", error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def send_message(self, message, *, idempotency_key):
        self.calls.append((message, idempotency_key))
        if self.error:
            raise self.error
        return self.result


async def queue_one():
    return await service.enqueue_notification(
        source_key="alert:alert-1",
        source_type="alert",
        source_id="alert-1",
        event_type="NEW_JOB",
        message="PLACEMENT ALERT\nCompany: Acme",
    )


def test_delivery_queue_deduplicates_by_stable_source_key(monkeypatch):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    patch_store(monkeypatch, store)

    first = run(queue_one())
    second = run(queue_one())

    assert first["id"] == second["id"]
    assert len(store.deliveries) == 1
    assert first["status"] == "pending"


def test_successful_send_persists_sent_state(monkeypatch):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    patch_store(monkeypatch, store)
    delivery = run(queue_one())
    provider = FakeProvider("wamid.accepted")

    result = run(service.dispatch_notification(delivery["id"], provider=provider, now=NOW))

    saved = store.deliveries["alert:alert-1"]
    assert result == "sent"
    assert saved["status"] == "sent"
    assert saved["provider_message_id"] == "wamid.accepted"
    assert saved["attempt_count"] == 1
    assert len(provider.calls) == 1
    assert provider.calls[0][1] == "alert:alert-1"


def test_transient_failure_retries_after_backoff_and_then_sends(monkeypatch):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    patch_store(monkeypatch, store)
    delivery = run(queue_one())
    flaky = FakeProvider(error=service.TransientNotificationError("http_503"))

    assert run(service.dispatch_pending_notifications(now=NOW, provider=flaky))["failed"] == 1
    saved = store.deliveries["alert:alert-1"]
    assert saved["status"] == "failed"
    assert saved["retryable"] is True
    assert saved["next_attempt_at"] == NOW + timedelta(seconds=60)

    assert run(service.dispatch_pending_notifications(now=NOW + timedelta(seconds=59), provider=FakeProvider()))["sent"] == 0
    later = FakeProvider("wamid.retry")
    assert run(service.dispatch_pending_notifications(now=NOW + timedelta(seconds=60), provider=later))["sent"] == 1
    assert store.deliveries["alert:alert-1"]["status"] == "sent"
    assert len(flaky.calls) == 1
    assert len(later.calls) == 1


def test_permanent_failure_is_recorded_without_retry(monkeypatch):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    patch_store(monkeypatch, store)
    delivery = run(queue_one())
    provider = FakeProvider(error=service.PermanentNotificationError("http_400"))

    assert run(service.dispatch_notification(delivery["id"], provider=provider, now=NOW)) == "failed"
    saved = store.deliveries["alert:alert-1"]
    assert saved["status"] == "failed"
    assert saved["retryable"] is False
    assert saved["error_code"] == "http_400"
    assert run(service.dispatch_pending_notifications(now=NOW + timedelta(days=1), provider=FakeProvider()))["sent"] == 0


def test_missing_configuration_marks_existing_pending_delivery_failed(monkeypatch):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    patch_store(monkeypatch, store)
    run(queue_one())
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN")

    result = run(service.run_notification_cycle(now=NOW))

    saved = store.deliveries["alert:alert-1"]
    assert result["status"] == "not_configured"
    assert saved["status"] == "failed"
    assert saved["retryable"] is False
    assert saved["error_code"] == "configuration_missing"


def test_disabled_cycle_does_not_read_database_or_send(monkeypatch):
    for key in (
        "WHATSAPP_ENABLED",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_API_VERSION",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_RECIPIENT_NUMBER",
    ):
        monkeypatch.delenv(key, raising=False)

    async def unexpected(*args, **kwargs):
        raise AssertionError("disabled notifications must not touch delivery data")

    monkeypatch.setattr(db, "fetch_all", unexpected)

    assert run(service.run_notification_cycle(now=NOW)) == {
        "status": "disabled", "queued": 0, "sent": 0, "failed": 0
    }


def test_due_reminder_is_queued_and_sent_once(monkeypatch):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    store.reminders = [{
        "id": "reminder-1",
        "kind": "attendance",
        "title": "Attendance: Acme",
        "description": "Confirm attendance by 5 PM.",
        "due_at": None,
        "status": "action_required",
        "severity": "rose",
        "source_id": "gmail-1",
        "source_type": "placement_event",
        "created_at": NOW,
    }]
    patch_store(monkeypatch, store)
    provider = FakeProvider("wamid.reminder")

    async def no_reminder_refresh(now=None):
        return []

    async def no_alert_queue():
        return 0

    monkeypatch.setattr(service, "sync_reminders_for_notifications", no_reminder_refresh)
    monkeypatch.setattr(service, "queue_alert_notifications", no_alert_queue)

    first = run(service.run_notification_cycle(now=NOW, provider=provider))
    second = run(service.run_notification_cycle(now=NOW, provider=provider))

    assert first["queued"] == 1
    assert first["sent"] == 1
    assert second["queued"] == 0
    assert second["sent"] == 0
    assert len(provider.calls) == 1
    assert "Confirm attendance by 5 PM." in provider.calls[0][0]
    assert "reminder:reminder-1" in provider.calls[0][1]


def test_failed_provider_exception_never_leaks_secrets_to_logs(monkeypatch, caplog):
    configure_whatsapp(monkeypatch)
    store = FakeStore()
    patch_store(monkeypatch, store)
    delivery = run(queue_one())
    provider = FakeProvider(error=RuntimeError(FAKE_SECRET))

    run(service.dispatch_notification(delivery["id"], provider=provider, now=NOW))

    assert FAKE_SECRET not in caplog.text
    assert store.deliveries["alert:alert-1"]["status"] == "failed"
    assert store.deliveries["alert:alert-1"]["error_code"] == "provider_error"


def test_health_reports_whatsapp_unconfigured_without_secrets(monkeypatch):
    for key in (
        "WHATSAPP_ENABLED",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_API_VERSION",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_RECIPIENT_NUMBER",
    ):
        monkeypatch.delenv(key, raising=False)

    result = run(placement.health())

    assert result["whatsapp_configured"] is False
    assert result["whatsapp_enabled"] is False
    assert FAKE_SECRET not in json.dumps(result)
