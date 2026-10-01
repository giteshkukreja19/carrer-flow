"""Parser and read-only safety checks against the captured Haveloc pages."""

import asyncio
from pathlib import Path

import pytest

from automation.haveloc import HavelocClient
from automation.parser import parse_job_details, parse_jobs, parse_participation


def test_jobs_page_fields_are_extracted_from_captured_table(haveloc_jobs_html: str) -> None:
    jobs = parse_jobs(haveloc_jobs_html)

    assert len(jobs) == 20
    assert jobs[0] == {
        "company": "Electronic Arts",
        "role": "Software Engineer",
        "salary_ctc": "21L",
        "salary_type": "Intern Leads to Full Time",
        "stipend": "50K",
        "stipend_period": "Per Month",
        "apply_by": "31 Aug 2026",
        "apply_by_time": None,
        "visit_date": "23 Sept 2026",
        "visit_date_note": "Tentative",
        "status": "In Progress",
        "batch": None,
        "posted_date": "19 Sept 2026",
    }
    assert jobs[1]["company"] == "ResNet Solutions Private Limited"
    assert jobs[1]["salary_ctc"] == "15L – 17L"
    assert jobs[1]["stipend"] == "25K"
    assert jobs[1]["status"] == "Closed For Applications"
    assert any(job["status"] == "Completed With Hiring" for job in jobs)
    assert any(job["status"] == "Completed Without Hiring" for job in jobs)
    assert all(job["batch"] is None for job in jobs)


def test_job_details_page_fields_are_extracted(haveloc_job_details_html: str) -> None:
    details = parse_job_details(haveloc_job_details_html)

    assert details["company"] == "Autodesk"
    assert details["role"] == "SDE / QA intern"
    assert details["role_type"] == "Intern"
    assert details["status"] == "In Progress"
    assert details["job_tags"] == ["Engineering"]
    assert details["metrics"] == [
        {"label": "Applicants", "value": "5,712", "meta": "Pipeline details unavailable"},
        {"label": "Stipend", "value": "₹45,000 – ₹65,000", "meta": "Per Month"},
        {"label": "Apply by", "value": "Expired", "meta": "11 Sep '26 by 9:00 AM"},
        {"label": "Date of visit", "value": None, "meta": None},
    ]
    assert details["hiring_process"] == [
        {"name": "Application Screening", "meta": "Rejected", "status": "Completed"},
        {"name": "Company Screening", "meta": "Upcoming", "status": "Completed"},
        {"name": "Test", "meta": "Upcoming · 1h 40m", "status": "On Going"},
        {"name": "Technical Interview", "meta": "Upcoming", "status": "Yet To Start"},
    ]
    assert "Internship Program Details" in details["job_description"]
    assert any(entry.startswith("graduating in 2027 from B.Tech") for entry in details["eligibility"])
    assert details["facts"] == [
        {
            "section": "Job",
            "items": [
                {"label": "Start date", "value": None},
                {"label": "Min hires", "value": None},
                {"label": "Expected offers", "value": None},
            ],
        },
        {
            "section": "Internship",
            "items": [
                {"label": "Mode", "value": "Bangalore and Pune"},
                {"label": "Start date", "value": None},
                {"label": "Duration", "value": "6 Months"},
                {"label": "Season", "value": "January"},
            ],
        },
    ]
    assert details["skills"] == [
        "JavaScript", "Python", "Java", "React", "Node.js", "SQL", "AWS",
        "TypeScript", "HTML/CSS", "Communication", "Problem Solving", "Teamwork",
    ]
    assert details["attachments"] == [
        {"name": "Autodesk_Internship_Open Positions_Jan-Jun2026.pdf", "href": None}
    ]


def test_participation_page_is_read_as_displayed(haveloc_participation_html: str) -> None:
    result = parse_participation(haveloc_participation_html)

    assert len(result["attendance"]) == 1
    assert result["attendance"][0] == {
        "company": "Toyota Connected",
        "role": "GET",
        "round": "Round 2 · Tech Talk",
        "status": "Attended",
        "meta": ["Wed 30 Sept, 19:00 – Wed 30 Sept, 22:00", "Manual Marking"],
        "note": None,
    }
    assert len(result["rounds"]) == 5
    assert result["rounds"][0] == {
        "company": "MathCo",
        "role": "AI Analyst",
        "round": "Round 2 · Test",
        "status": "Expired",
        "meta": ["Wed, 2 Sept 2026 · 9:00 AM", "Virtual"],
        "note": "This round has already started.",
    }


def test_tracker_fixture_coverage_is_explicitly_missing() -> None:
    tracker_fixture = Path(__file__).parent / "fixtures" / "haveloc_tracker.html"
    if not tracker_fixture.exists():
        pytest.skip("haveloc_tracker.html is missing; tracker coverage is unavailable")
    pytest.fail("A tracker fixture was found, but no tracker parser coverage has been added")


def test_haveloc_client_scan_remains_paused() -> None:
    result = asyncio.run(HavelocClient().scan())
    assert result["status"] == "paused"
