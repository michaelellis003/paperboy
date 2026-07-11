import httpx
import pytest

import paperboy.config
from paperboy.models import Paper


@pytest.fixture
def env(monkeypatch):
    """Email-delivery environment, with the settings cache reset."""
    monkeypatch.setenv("DELIVERY_METHOD", "email")
    monkeypatch.setenv("DEVICE_EMAIL", "reader@kindle.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "user@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2")
    monkeypatch.setenv("FROM_EMAIL", "user@example.com")
    monkeypatch.setattr(paperboy.config, "_settings", None)
    yield monkeypatch
    monkeypatch.setattr(paperboy.config, "_settings", None)


@pytest.fixture
def paper_factory():
    """Build Paper objects with sensible defaults."""

    def make(**overrides) -> Paper:
        fields = {
            "title": "A Test Paper",
            "authors": ["Ada Lovelace"],
            "abstract": "An abstract.",
            "published": "2024-01-20",
            "url": "https://arxiv.org/abs/2401.12345",
            "pdf_url": "https://arxiv.org/pdf/2401.12345",
            "arxiv_id": "2401.12345",
            "doi": None,
        }
        fields.update(overrides)
        return Paper(**fields)

    return make


def json_client(handler) -> httpx.Client:
    """An httpx client whose responses come from ``handler(request)``."""
    return httpx.Client(transport=httpx.MockTransport(handler))
