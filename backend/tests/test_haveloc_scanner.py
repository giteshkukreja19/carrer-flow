"""Mock-only scanner tests; no Haveloc network access is made."""

import asyncio
from pathlib import Path

import pytest

from automation.haveloc import FetchResult, HavelocClient, scanner_configured
from automation.parser import parse_job_details, parse_jobs, parse_participation
from lib import db
from services.scan_service import _persist_scan

FIXTURES = Path(__file__).parent / "fixtures"


def async_test(function):
    from functools import wraps

    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def configure(monkeypatch):
    origin = "https://placements.haveloc.com"
    monkeypatch.setenv("HAVELOC_SCAN_ENABLED", "true")
    monkeypatch.setenv("HAVELOC_BASE_URL", origin + "/")
    monkeypatch.setenv("HAVELOC_JOBS_URL", origin + "/jobs")
    monkeypatch.setenv("HAVELOC_JOB_DETAILS_URLS", origin + "/jobs/details?jid=mock")
    monkeypatch.setenv("HAVELOC_PARTICIPATION_URL", origin + "/tracker")
    monkeypatch.setenv("HAVELOC_SESSION_COOKIE_NAME", "mock-session")
    monkeypatch.setenv("HAVELOC_SESSION_COOKIE", "opaque-test-value")


@async_test
async def test_scan_is_disabled_by_default_and_does_not_fetch(monkeypatch):
    monkeypatch.delenv("HAVELOC_SCAN_ENABLED", raising=False)
    called = False

    async def fetcher(*_):
        nonlocal called
        called = True
        return FetchResult(200, "")

    result = await HavelocClient(fetcher=fetcher).scan()
    assert result["status"] == "disabled"
    assert not called


@async_test
async def test_scanner_parses_captured_pages_using_get_only(monkeypatch):
    configure(monkeypatch)
    pages = {
        "https://placements.haveloc.com/jobs": (FIXTURES / "haveloc_jobs.html").read_text(),
        "https://placements.haveloc.com/jobs/details?jid=mock": (FIXTURES / "haveloc_job_details.html").read_text(),
        "https://placements.haveloc.com/tracker": (FIXTURES / "haveloc_participation.html").read_text(),
    }
    calls = []

    async def fetcher(url, cookie):
        calls.append((url, cookie))
        return FetchResult(200, pages[url])

    assert scanner_configured()
    result = await HavelocClient(fetcher=fetcher).scan()
    assert result["status"] == "ok"
    assert len(result["jobs"]) == 20
    assert result["job_details"][0]["details"]["company"] == "Autodesk"
    assert result["participation"]["attendance"][0]["status"] == "Attended"
    assert result["participation"]["rounds"][0]["status"] == "Expired"
    assert len(calls) == 3
    assert all(cookie == "mock-session=opaque-test-value" for _, cookie in calls)


@async_test
@pytest.mark.parametrize("result", [
    FetchResult(200, "<html>CAPTCHA verification required</html>"),
    FetchResult(403, "<html>access denied</html>"),
    FetchResult(302, "", "/login"),
])
async def test_challenges_stop_before_reading_additional_pages(monkeypatch, result):
    configure(monkeypatch)
    calls = []

    async def fetcher(url, _cookie):
        calls.append(url)
        return result

    scanned = await HavelocClient(fetcher=fetcher).scan()
    assert scanned["status"] == "manual_intervention_required"
    assert scanned["attention_required"] is True
    assert len(calls) == 1


@async_test
async def test_cross_origin_page_configuration_is_rejected(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("HAVELOC_PARTICIPATION_URL", "https://other.example/tracker")

    scanned = await HavelocClient().scan()
    assert scanned["status"] == "not_configured"


@async_test
async def test_action_shaped_page_urls_are_rejected_without_fetch(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("HAVELOC_PARTICIPATION_URL", "https://placements.haveloc.com/tracker/%61ttendance/mark")
    called = False

    async def fetcher(*_):
        nonlocal called
        called = True
        return FetchResult(200, "")

    result = await HavelocClient(fetcher=fetcher).scan()
    assert result["status"] == "not_configured"
    assert called is False


@async_test
async def test_unrecognized_jobs_dom_stops_before_writing_or_fetching_more_pages(monkeypatch):
    configure(monkeypatch)
    calls = []

    async def fetcher(url, _cookie):
        calls.append(url)
        return FetchResult(200, "<html><title>Login</title></html>")

    result = await HavelocClient(fetcher=fetcher).scan()
    assert result["status"] == "error"
    assert result["attention_required"] is True
    assert len(calls) == 1


def test_scanner_readiness_requires_explicit_flag_urls_and_session(monkeypatch):
    configure(monkeypatch)
    assert scanner_configured()
    monkeypatch.setenv("HAVELOC_SCAN_ENABLED", "false")
    assert not scanner_configured()
    monkeypatch.setenv("HAVELOC_SCAN_ENABLED", "true")
    monkeypatch.setenv("HAVELOC_SESSION_COOKIE", "")
    assert not scanner_configured()


def test_scan_persistence_uses_stable_read_source_ids_and_never_creates_applications(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(db, "pool", object())
    executions = []

    async def execute(query, *args):
        executions.append((query, args))
        return True

    monkeypatch.setattr(db, "execute", execute)
    jobs = parse_jobs((FIXTURES / "haveloc_jobs.html").read_text())
    details = parse_job_details((FIXTURES / "haveloc_job_details.html").read_text())
    participation = parse_participation((FIXTURES / "haveloc_participation.html").read_text())
    result = {
        "jobs": jobs,
        "job_details": [{"url": "fixture", "details": details}],
        "participation": participation,
    }

    counts = asyncio.run(_persist_scan(result, datetime(2026, 10, 1, tzinfo=timezone.utc)))
    assert counts["jobs"] == sum(bool(job.get("company") and job.get("role")) for job in jobs)
    assert counts["attendance"] == 1
    assert all("INSERT INTO applications" not in query for query, _ in executions)
    attendance_writes = [(query, args) for query, args in executions if "INSERT INTO attendance" in query]
    assert len(attendance_writes) == 1
    assert attendance_writes[0][1][5] == "Attended"  # copied from displayed HTML, not generated by the scanner

    first_ids = [args[0] for query, args in executions if "INSERT INTO jobs" in query or "INSERT INTO rounds" in query or "INSERT INTO attendance" in query]
    executions.clear()
    asyncio.run(_persist_scan(result, datetime(2026, 10, 2, tzinfo=timezone.utc)))
    retry_ids = [args[0] for query, args in executions if "INSERT INTO jobs" in query or "INSERT INTO rounds" in query or "INSERT INTO attendance" in query]
    assert first_ids == retry_ids
    assert all("ON CONFLICT" in query for query, _ in executions if "INSERT INTO jobs" in query or "INSERT INTO rounds" in query or "INSERT INTO attendance" in query)
