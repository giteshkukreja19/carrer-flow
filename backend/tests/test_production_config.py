from services import gmail
from server import LOCAL_CORS_ORIGINS, configured_cors_origins
from routers import placement


def test_cors_defaults_to_local_origins_only():
    assert configured_cors_origins("") == list(LOCAL_CORS_ORIGINS)


def test_cors_accepts_comma_separated_explicit_origins():
    assert configured_cors_origins("https://ui.example.test/,https://admin.example.test") == [
        "https://ui.example.test",
        "https://admin.example.test",
    ]


def test_cors_rejects_wildcard_origins():
    try:
        configured_cors_origins("*")
    except ValueError as exc:
        assert "explicit origins" in str(exc)
    else:
        raise AssertionError("wildcard origins must be rejected")


def test_gmail_redirect_uri_supports_separate_production_backend(monkeypatch):
    monkeypatch.setenv("GMAIL_REDIRECT_URI", "https://api.example.test/api/gmail/oauth/callback")
    monkeypatch.setenv("APP_URL", "https://ui.example.test")
    assert gmail.redirect_uri() == "https://api.example.test/api/gmail/oauth/callback"


def test_gmail_redirect_uri_keeps_local_proxy_default(monkeypatch):
    monkeypatch.delenv("GMAIL_REDIRECT_URI", raising=False)
    monkeypatch.setenv("APP_URL", "http://localhost:5173")
    assert gmail.redirect_uri() == "http://localhost:5173/api/gmail/oauth/callback"


def test_health_is_available_without_optional_integrations(monkeypatch):
    for name in (
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GMAIL_TOKEN_ENCRYPTION_KEY",
        "GEMINI_API_KEY",
        "WHATSAPP_ENABLED",
        "WHATSAPP_PROVIDER",
        "WHATSAPP_API_VERSION",
        "WHATSAPP_PHONE_NUMBER_ID",
        "WHATSAPP_ACCESS_TOKEN",
        "WHATSAPP_RECIPIENT_NUMBER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(placement.db, "pool", None)

    import asyncio

    result = asyncio.run(placement.health())

    assert result["status"] == "ok"
    assert result["gmail_oauth_configured"] is False
    assert result["gemini_configured"] is False
    assert result["whatsapp_configured"] is False
    assert result["whatsapp_enabled"] is False
