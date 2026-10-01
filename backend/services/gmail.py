"""Read-only Gmail OAuth and message access.

Email messages are untrusted source data. This module retrieves and stores no
more than message metadata and body text; it never interprets email instructions.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.parse import urljoin

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

from lib import db
from services.llm_classifier import gemini_configured

load_dotenv()

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = [GMAIL_READONLY_SCOPE]
_logger = logging.getLogger(__name__)
_SENDER_FORBIDDEN = set('<>()[],;:"') | {"\\"}


class GmailIntegrationError(Exception):
    """Base class for safe, user-facing Gmail integration failures."""


class GmailConfigurationError(GmailIntegrationError):
    pass


class GmailAuthorizationError(GmailIntegrationError):
    pass


class GmailServiceError(GmailIntegrationError):
    pass


class GmailStorageError(GmailIntegrationError):
    pass


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    received_at: datetime
    body: str


def oauth_client_configured() -> bool:
    return bool(
        os.environ.get("GOOGLE_CLIENT_ID", "").strip()
        and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    )


def _fernet() -> Fernet:
    key = os.environ.get("GMAIL_TOKEN_ENCRYPTION_KEY", "").strip()
    if not key:
        raise GmailConfigurationError("GMAIL_TOKEN_ENCRYPTION_KEY is not configured.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise GmailConfigurationError("GMAIL_TOKEN_ENCRYPTION_KEY is invalid.") from None


def token_encryption_configured() -> bool:
    try:
        _fernet()
        return True
    except GmailConfigurationError:
        return False


def oauth_configured() -> bool:
    """Whether the OAuth client and token encryption key are ready for use."""
    return oauth_client_configured() and token_encryption_configured()


def redirect_uri() -> str:
    configured = os.environ.get("GMAIL_REDIRECT_URI", "").strip()
    if configured:
        return configured
    app_url = os.environ.get("APP_URL", "http://localhost:5173").rstrip("/") + "/"
    return urljoin(app_url, "api/gmail/oauth/callback")


def _client_config() -> dict[str, dict[str, str]]:
    if not oauth_client_configured():
        raise GmailConfigurationError("Google OAuth client credentials are not configured.")
    return {
        "web": {
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }


def _encrypt_token(value: str) -> str:
    try:
        return _fernet().encrypt(value.encode("utf-8")).decode("ascii")
    except GmailConfigurationError:
        raise
    except Exception:
        raise GmailConfigurationError("Gmail token encryption failed.") from None


def _decrypt_token(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except GmailConfigurationError:
        raise
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError, ValueError, TypeError):
        raise GmailAuthorizationError("Stored Gmail authorization is invalid. Reconnect read-only Gmail access.") from None


def validate_sender(sender: str) -> str:
    """Accept one exact email address, never a display name or Gmail query fragment."""
    normalized = sender.strip()
    address = parseaddr(normalized)[1]
    local, separator, domain = normalized.partition("@")
    if (
        not normalized
        or address.casefold() != normalized.casefold()
        or not separator
        or not local
        or not domain
        or normalized.count("@") != 1
        or any(character.isspace() or character in _SENDER_FORBIDDEN for character in normalized)
    ):
        raise GmailConfigurationError("Set the exact Haveloc notification sender email address in Settings.")
    return normalized


async def create_authorization() -> tuple[str, str]:
    if not oauth_client_configured():
        raise GmailConfigurationError("Google OAuth client credentials are not configured.")
    _fernet()  # Fail before creating OAuth state if tokens cannot be encrypted.
    if db.pool is None:
        raise GmailStorageError("PostgreSQL must be available before Gmail can be connected.")

    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(_client_config(), scopes=GMAIL_SCOPES, redirect_uri=redirect_uri())
        url, state = flow.authorization_url(
            access_type="offline",
            prompt="consent",
        )
    except GmailIntegrationError:
        raise
    except Exception:
        raise GmailConfigurationError("Google OAuth could not be initialized. Check the OAuth client configuration.") from None

    try:
        saved = await db.execute(
            "INSERT INTO gmail_oauth_states (state, expires_at) VALUES ($1,$2)",
            state,
            datetime.now(timezone.utc) + timedelta(minutes=10),
        )
    except Exception:
        raise GmailStorageError("PostgreSQL could not save Gmail authorization state.") from None
    if not saved:
        raise GmailStorageError("PostgreSQL could not save Gmail authorization state.")
    return url, state


async def complete_authorization(code: str, state: str) -> bool:
    if not oauth_client_configured():
        raise GmailConfigurationError("Google OAuth client credentials are not configured.")
    _fernet()
    if db.pool is None:
        raise GmailStorageError("PostgreSQL must be available to finish Gmail authorization.")

    try:
        state_row = await db.fetch_one(
            "SELECT state FROM gmail_oauth_states WHERE state=$1 AND expires_at > NOW()",
            state,
        )
    except Exception:
        raise GmailStorageError("PostgreSQL could not validate Gmail authorization state.") from None
    if not state_row:
        return False

    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(_client_config(), scopes=GMAIL_SCOPES, redirect_uri=redirect_uri())
        await asyncio.to_thread(flow.fetch_token, code=code)
        credentials = flow.credentials
    except Exception:
        # OAuth libraries can include authorization codes or endpoint details in
        # exceptions. Keep those values out of API responses and application logs.
        raise GmailAuthorizationError("Google rejected the Gmail authorization. Start read-only Gmail authorization again.") from None

    access_token = getattr(credentials, "token", None)
    if not access_token:
        raise GmailAuthorizationError("Google did not return a usable Gmail access token.")
    granted_scopes = list(getattr(credentials, "scopes", None) or GMAIL_SCOPES)
    if set(granted_scopes) != {GMAIL_READONLY_SCOPE}:
        raise GmailAuthorizationError("Google did not grant the required Gmail read-only permission.")

    encrypted_access = _encrypt_token(access_token)
    refresh_token = getattr(credentials, "refresh_token", None)
    encrypted_refresh = _encrypt_token(refresh_token) if refresh_token else None
    try:
        saved_token = await db.execute(
            "INSERT INTO gmail_tokens (id, access_token, refresh_token, expires_at, scope) "
            "VALUES (1,$1,$2,$3,$4) ON CONFLICT (id) DO UPDATE SET "
            "access_token=EXCLUDED.access_token, "
            "refresh_token=COALESCE(EXCLUDED.refresh_token, gmail_tokens.refresh_token), "
            "expires_at=EXCLUDED.expires_at, scope=EXCLUDED.scope",
            encrypted_access,
            encrypted_refresh,
            getattr(credentials, "expiry", None),
            " ".join(granted_scopes),
        )
    except Exception:
        raise GmailStorageError("PostgreSQL could not save the encrypted Gmail authorization.") from None
    if not saved_token:
        raise GmailStorageError("PostgreSQL could not save the encrypted Gmail authorization.")
    try:
        state_deleted = await db.execute("DELETE FROM gmail_oauth_states WHERE state=$1", state)
    except Exception:
        raise GmailStorageError("PostgreSQL could not finish Gmail authorization safely.") from None
    if not state_deleted:
        raise GmailStorageError("PostgreSQL could not finish Gmail authorization safely.")
    return True


async def _credentials():
    if not oauth_configured():
        return None
    if db.pool is None:
        raise GmailStorageError("PostgreSQL is unavailable for Gmail authorization data.")
    try:
        row = await db.fetch_one(
            "SELECT access_token, refresh_token, expires_at, scope FROM gmail_tokens WHERE id=1"
        )
    except Exception:
        raise GmailStorageError("PostgreSQL could not read Gmail authorization data.") from None
    if not row:
        return None

    stored_scopes = (row.get("scope") or "").split()
    if GMAIL_READONLY_SCOPE not in stored_scopes:
        raise GmailAuthorizationError("Stored Gmail authorization lacks the required read-only permission. Reconnect Gmail.")

    try:
        from google.oauth2.credentials import Credentials

        credentials = Credentials(
            token=_decrypt_token(row["access_token"]),
            refresh_token=_decrypt_token(row["refresh_token"]) if row.get("refresh_token") else None,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            scopes=stored_scopes,
            expiry=row.get("expires_at"),
        )
    except GmailIntegrationError:
        raise
    except Exception:
        raise GmailAuthorizationError("Stored Gmail authorization is invalid. Reconnect read-only Gmail access.") from None

    if credentials.expired:
        if not credentials.refresh_token:
            raise GmailAuthorizationError("Gmail access has expired. Reconnect read-only Gmail access.")
        try:
            from google.auth.transport.requests import Request

            await asyncio.to_thread(credentials.refresh, Request())
        except Exception:
            raise GmailAuthorizationError("Gmail authorization expired or was revoked. Reconnect read-only Gmail access.") from None
        try:
            updated = await db.execute(
                "UPDATE gmail_tokens SET access_token=$1, expires_at=$2 WHERE id=1",
                _encrypt_token(credentials.token or ""),
                credentials.expiry,
            )
        except Exception:
            raise GmailStorageError("PostgreSQL could not save the refreshed Gmail authorization.") from None
        if not updated:
            raise GmailStorageError("PostgreSQL could not save the refreshed Gmail authorization.")
    return credentials


def _decode_body(payload: dict) -> str:
    """Decode the first textual body part, treating malformed MIME as empty text."""
    if not isinstance(payload, dict):
        return ""
    if payload.get("filename"):
        return ""
    mime_type = payload.get("mimeType")
    if mime_type and mime_type not in {"text/plain", "text/html", "multipart/alternative", "multipart/mixed"}:
        return ""
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            if part.get("mimeType") == "text/plain":
                decoded = _decode_body(part)
                if decoded:
                    return decoded
        for part in parts:
            if isinstance(part, dict):
                decoded = _decode_body(part)
                if decoded:
                    return decoded
    body = payload.get("body")
    if not isinstance(body, dict):
        return ""
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _header_map(payload: dict) -> dict[str, str]:
    raw_headers = payload.get("headers", [])
    if not isinstance(raw_headers, list):
        return {}
    headers: dict[str, str] = {}
    for header in raw_headers:
        if not isinstance(header, dict):
            continue
        name = header.get("name")
        value = header.get("value")
        if isinstance(name, str) and isinstance(value, str):
            headers[name.casefold()] = value
    return headers


def _parse_gmail_message(raw: dict, exact_sender: str) -> GmailMessage | None:
    if not isinstance(raw, dict):
        return None
    message_id = raw.get("id")
    payload = raw.get("payload")
    if not isinstance(message_id, str) or not message_id.strip() or not isinstance(payload, dict):
        return None
    headers = _header_map(payload)
    sender = parseaddr(headers.get("from", ""))[1]
    if not sender or sender.casefold() != exact_sender.casefold():
        return None
    try:
        internal_ms = int(raw["internalDate"])
        if internal_ms <= 0:
            return None
        received_at = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        return None

    subject = headers.get("subject", "")
    if not isinstance(subject, str):
        subject = ""
    thread_id = raw.get("threadId")
    if not isinstance(thread_id, str):
        thread_id = None
    return GmailMessage(
        message_id=message_id.strip(),
        thread_id=thread_id,
        sender=sender.strip(),
        subject=subject,
        received_at=received_at,
        body=_decode_body(payload),
    )


async def fetch_messages(sender: str, since: datetime | None = None) -> list[GmailMessage]:
    exact_sender = validate_sender(sender)
    credentials = await _credentials()
    if credentials is None:
        raise GmailAuthorizationError("Gmail is not connected. Complete read-only Gmail authorization first.")

    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    # Re-query the previous second and rely on Gmail message-ID deduplication to
    # avoid missing multiple messages with the same timestamp at poll boundaries.
    after = f" after:{max(0, int(since.timestamp()) - 1)}" if since else ""
    query = f"from:{exact_sender}{after}"

    def read_messages() -> list[GmailMessage]:
        try:
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            messages: list[GmailMessage] = []
            seen_ids: set[str] = set()
            page_token = None
            while True:
                request = service.users().messages().list(
                    userId="me", q=query, maxResults=100, pageToken=page_token
                )
                listing = request.execute()
                for item in listing.get("messages", []) or []:
                    if not isinstance(item, dict):
                        continue
                    message_id = item.get("id")
                    if not isinstance(message_id, str) or not message_id or message_id in seen_ids:
                        continue
                    seen_ids.add(message_id)
                    raw = service.users().messages().get(
                        userId="me", id=message_id, format="full"
                    ).execute()
                    parsed = _parse_gmail_message(raw, exact_sender)
                    if parsed is None:
                        _logger.warning("Skipping malformed or non-matching Gmail message")
                        continue
                    messages.append(parsed)
                page_token = listing.get("nextPageToken")
                if not page_token:
                    break
            return messages
        except HttpError as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 401:
                raise GmailAuthorizationError("Gmail authorization expired or was revoked. Reconnect read-only Gmail access.") from None
            if status == 403:
                raise GmailServiceError("Gmail rejected the read-only request. Check API access and account consent.") from None
            raise GmailServiceError("Gmail could not return messages. Retry after checking Google API availability.") from None
        except GmailIntegrationError:
            raise
        except Exception:
            raise GmailServiceError("Gmail could not return messages. Retry after checking Google API availability.") from None

    return await asyncio.to_thread(read_messages)


async def integration_status(sender: str, polling_enabled: bool, last_polled_at: datetime | None):
    try:
        token_row = await db.fetch_one("SELECT id FROM gmail_tokens WHERE id=1") if db.pool is not None else None
    except Exception:
        raise GmailStorageError("PostgreSQL could not read Gmail connection status.") from None
    configured = oauth_configured()
    connected = bool(token_row) and configured
    sender_ready = False
    try:
        validate_sender(sender)
        sender_ready = True
    except GmailConfigurationError:
        pass
    return {
        "oauth_configured": configured,
        # Kept for the existing dashboard's API contract.
        "gmail_oauth_configured": configured,
        "gmail_connected": connected,
        "gemini_configured": gemini_configured(),
        "sender_configured": sender_ready,
        "polling_enabled": bool(polling_enabled and configured and connected and sender_ready),
        "last_polled_at": last_polled_at,
    }


__all__ = [
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SCOPES",
    "GmailAuthorizationError",
    "GmailConfigurationError",
    "GmailIntegrationError",
    "GmailMessage",
    "GmailServiceError",
    "GmailStorageError",
    "_decrypt_token",
    "_encrypt_token",
    "complete_authorization",
    "create_authorization",
    "fetch_messages",
    "integration_status",
    "oauth_client_configured",
    "oauth_configured",
    "redirect_uri",
    "token_encryption_configured",
    "validate_sender",
]
