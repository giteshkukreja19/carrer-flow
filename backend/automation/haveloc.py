"""Opt-in Haveloc page reader.

The client performs HTTP GET requests only. It never submits forms or follows
redirects. A manually provisioned same-origin session cookie is required before
a scan starts; expired sessions and anti-bot challenges stop for manual
intervention.
"""

from __future__ import annotations

import asyncio
import os
import re
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlparse

from automation.parser import parse_job_details, parse_jobs, parse_participation

MAX_PAGE_BYTES = 8 * 1024 * 1024
MAX_JOB_DETAIL_PAGES = 50
REQUEST_TIMEOUT_SECONDS = 15
_ANTI_BOT_MARKERS = (
    "captcha",
    "verify you are human",
    "verify that you are not a robot",
    "access denied",
    "unusual traffic",
    "robot check",
    "automated requests",
    "challenge-platform",
    "cf-chl-",
)
_ACTION_PATHS = {
    "apply", "submit", "attendance", "mark", "confirm", "withdraw", "logout",
    "delete", "update", "edit", "save", "action", "answer", "register",
    "accept", "decline", "cancel",
}
_ACTION_QUERY_KEYS = {"action", "apply", "submit", "attendance", "mark", "confirm", "operation", "intent"}
_COOKIE_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=%-]{1,4096}$")


@dataclass(frozen=True)
class FetchResult:
    status_code: int
    body: str
    location: str | None = None


async def _http_get(url: str, cookie_header: str | None) -> FetchResult:
    """Fetch a single HTML page without following redirects or sending writes."""

    return await asyncio.to_thread(_blocking_get, url, cookie_header)


def _blocking_get(url: str, cookie_header: str | None) -> FetchResult:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    headers = {"Accept": "text/html", "User-Agent": "CareerFlow-ReadOnly/1.0"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    request = urllib.request.Request(url, headers=headers, method="GET")
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise RuntimeError("Haveloc returned a non-HTML page.")
            body_bytes = response.read(MAX_PAGE_BYTES + 1)
            if len(body_bytes) > MAX_PAGE_BYTES:
                raise RuntimeError("Haveloc page exceeded the safe response size limit.")
            return FetchResult(response.status, body_bytes.decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        body = exc.read(MAX_PAGE_BYTES + 1).decode("utf-8", errors="replace")
        return FetchResult(exc.code, body[:MAX_PAGE_BYTES], exc.headers.get("Location"))
    except urllib.error.URLError as exc:
        raise RuntimeError("Haveloc could not be reached.") from exc


def scan_enabled() -> bool:
    return os.environ.get("HAVELOC_SCAN_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _origin(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Haveloc page URLs must be absolute HTTP(S) URLs without embedded credentials.")
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Haveloc page URLs must use HTTPS.")
    return (parsed.scheme.lower(), parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80))


def configured_page_urls() -> tuple[str, list[str], str] | None:
    jobs_url = os.environ.get("HAVELOC_JOBS_URL", "").strip()
    details_raw = os.environ.get("HAVELOC_JOB_DETAILS_URLS", "").strip()
    participation_url = os.environ.get("HAVELOC_PARTICIPATION_URL", "").strip()
    details_urls = [value.strip() for value in details_raw.split(",") if value.strip()]
    if not jobs_url or not details_urls or not participation_url:
        return None
    if len(details_urls) > MAX_JOB_DETAIL_PAGES:
        raise ValueError(f"Configure no more than {MAX_JOB_DETAIL_PAGES} detail pages per scan.")
    expected = _origin(os.environ.get("HAVELOC_BASE_URL", "https://placements.haveloc.com/").strip())
    urls = [jobs_url, *details_urls, participation_url]
    if any(_origin(url) != expected for url in urls):
        raise ValueError("Every Haveloc scan page must use the configured base origin.")
    if unquote(urlparse(jobs_url).path).rstrip("/") != "/jobs":
        raise ValueError("The jobs page URL must use the captured /jobs page path.")
    if any(unquote(urlparse(url).path).rstrip("/") != "/jobs/details" for url in details_urls):
        raise ValueError("Job detail URLs must use the captured /jobs/details page path.")
    for url in (jobs_url, *details_urls, participation_url):
        parsed = urlparse(url)
        path_parts = {part.casefold() for part in unquote(parsed.path).split("/") if part}
        query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if parsed.fragment or ";" in parsed.path or path_parts & _ACTION_PATHS or query_keys & _ACTION_QUERY_KEYS:
            raise ValueError("A configured Haveloc URL resembles an action endpoint and cannot be scanned.")
    return jobs_url, details_urls, participation_url


def scanner_configured() -> bool:
    if not scan_enabled():
        return False
    try:
        urls = configured_page_urls()
    except ValueError:
        return False
    cookie_name = os.environ.get("HAVELOC_SESSION_COOKIE_NAME", "").strip()
    cookie_value = os.environ.get("HAVELOC_SESSION_COOKIE", "").strip()
    safe_cookie = bool(_COOKIE_NAME.fullmatch(cookie_name) and _COOKIE_VALUE.fullmatch(cookie_value))
    return urls is not None and safe_cookie


class HavelocClient:
    """Read jobs/details/participation from explicitly configured page URLs."""

    def __init__(
        self,
        *,
        fetcher: Callable[[str, str | None], Awaitable[FetchResult]] | None = None,
    ) -> None:
        self._fetcher = fetcher or _http_get

    def _cookie_header(self) -> str | None:
        name = os.environ.get("HAVELOC_SESSION_COOKIE_NAME", "").strip()
        value = os.environ.get("HAVELOC_SESSION_COOKIE", "").strip()
        if not name and not value:
            return None
        if not _COOKIE_NAME.fullmatch(name) or not _COOKIE_VALUE.fullmatch(value):
            raise ValueError("The configured Haveloc session cookie is invalid.")
        return f"{name}={value}"

    async def _read_page(self, url: str, cookie: str | None) -> str:
        result = await self._fetcher(url, cookie)
        body_lower = result.body.casefold()
        if result.status_code in {401, 403, 429} or result.location:
            raise PermissionError("Haveloc authorization or access challenge requires manual intervention.")
        if any(marker in body_lower for marker in _ANTI_BOT_MARKERS):
            raise PermissionError("Haveloc anti-bot verification requires manual intervention.")
        if result.status_code < 200 or result.status_code >= 300:
            raise RuntimeError(f"Haveloc returned HTTP {result.status_code}.")
        return result.body

    async def scan(self) -> dict[str, object]:
        if not scan_enabled():
            return {"status": "disabled", "message": "Haveloc scanning is disabled by default."}
        try:
            urls = configured_page_urls()
            cookie = self._cookie_header()
        except ValueError as exc:
            return {"status": "not_configured", "message": str(exc)}
        if urls is None or cookie is None:
            return {
                "status": "not_configured",
                "message": "Configure exact Haveloc page URLs and an authenticated session cookie before scanning.",
            }

        jobs_url, details_urls, participation_url = urls
        try:
            jobs_html = await self._read_page(jobs_url, cookie)
            jobs = parse_jobs(jobs_html)
            if not jobs:
                return {
                    "status": "error",
                    "message": "The configured jobs page did not match the recovered read-only DOM; no records were changed.",
                    "attention_required": True,
                }
            detail_pages = []
            for url in details_urls:
                html = await self._read_page(url, cookie)
                details = parse_job_details(html)
                if not details.get("company") or not details.get("role"):
                    return {
                        "status": "error",
                        "message": "A configured job details page did not match the recovered read-only DOM; no records were changed.",
                        "attention_required": True,
                    }
                detail_pages.append({"url": url, "details": details})
            participation_html = await self._read_page(participation_url, cookie)
            participation = parse_participation(participation_html)
        except PermissionError as exc:
            return {"status": "manual_intervention_required", "message": str(exc), "attention_required": True}
        except Exception as exc:
            # Do not include request headers, cookie values, or full response bodies.
            return {
                "status": "error",
                "message": f"Haveloc scan stopped safely ({type(exc).__name__}); verify read-only configuration and retry.",
                "attention_required": True,
            }

        return {
            "status": "ok",
            "message": "Haveloc pages were read without submitting forms or changing attendance.",
            "jobs": jobs,
            "job_details": detail_pages,
            "participation": participation,
        }
