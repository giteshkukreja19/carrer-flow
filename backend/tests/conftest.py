"""Shared access to the recovered, real Haveloc HTML captures."""

from pathlib import Path

import pytest


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def haveloc_jobs_html() -> str:
    return (FIXTURES / "haveloc_jobs.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def haveloc_job_details_html() -> str:
    return (FIXTURES / "haveloc_job_details.html").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def haveloc_participation_html() -> str:
    return (FIXTURES / "haveloc_participation.html").read_text(encoding="utf-8")
