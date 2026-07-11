"""Configuration from environment variables.

All secrets (SMTP credentials, Dropbox tokens, Zotero API key, device
address) come from the environment so they can live in Cloud Run secrets
rather than in code or chat. Nothing is required at import time; each
delivery backend validates what it needs when it is used.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int_env(name: str, default: str) -> int:
    value = _env(name, default)
    try:
        return int(value)
    except ValueError:
        raise RuntimeError(
            f"{name} must be a number, got {value!r} — fix it in .env "
            "or re-run 'paperboy setup'."
        ) from None


def load_env_file(path: str | None = None) -> None:
    """Load .env into the environment; already-set variables win.

    Called from the server entry point so that the file the setup
    wizard writes actually takes effect. Defaults to ./.env (the cwd
    that ``uv run --directory <project>`` sets); override the location
    with PAPERBOY_ENV.
    """
    load_dotenv(path or os.environ.get("PAPERBOY_ENV", ".env"), override=False)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, read once from the environment."""

    # Delivery: "email" (Kindle, PocketBook, ...) or "dropbox" (Kobo)
    delivery_method: str = field(
        default_factory=lambda: _env("DELIVERY_METHOD", "email")
    )

    # Email backend. DEVICE_EMAIL is the e-reader's intake address;
    # KINDLE_EMAIL is accepted as an alias. The sender must be approved
    # by the vendor (e.g. Kindle's Approved Personal Document list).
    device_email: str = field(
        default_factory=lambda: _env("DEVICE_EMAIL") or _env("KINDLE_EMAIL")
    )
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int_env("SMTP_PORT", "465"))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    from_email: str = field(default_factory=lambda: _env("FROM_EMAIL"))

    # Dropbox backend (Kobo syncs a linked Dropbox folder natively)
    dropbox_app_key: str = field(
        default_factory=lambda: _env("DROPBOX_APP_KEY")
    )
    dropbox_app_secret: str = field(
        default_factory=lambda: _env("DROPBOX_APP_SECRET")
    )
    dropbox_refresh_token: str = field(
        default_factory=lambda: _env("DROPBOX_REFRESH_TOKEN")
    )
    dropbox_folder: str = field(
        default_factory=lambda: _env("DROPBOX_FOLDER", "/Books")
    )

    # Zotero (optional — delivery works without it)
    zotero_api_key: str = field(default_factory=lambda: _env("ZOTERO_API_KEY"))
    zotero_library_id: str = field(
        default_factory=lambda: _env("ZOTERO_LIBRARY_ID")
    )
    zotero_library_type: str = field(
        default_factory=lambda: _env("ZOTERO_LIBRARY_TYPE", "user")
    )
    reading_queue_collection: str = field(
        default_factory=lambda: _env(
            "READING_QUEUE_COLLECTION", "Reading Queue"
        )
    )
    sent_tag: str = field(
        default_factory=lambda: _env("SENT_TAG", "sent-to-ereader")
    )

    # Polite-pool contact email for OpenAlex/Unpaywall; falls back to
    # FROM_EMAIL when unset.
    contact_email: str = field(default_factory=lambda: _env("CONTACT_EMAIL"))

    @property
    def zotero_enabled(self) -> bool:
        """Whether Zotero credentials are configured."""
        return bool(self.zotero_api_key and self.zotero_library_id)

    @property
    def polite_email(self) -> str:
        """Contact email sent to polite-pool APIs (OpenAlex, Unpaywall)."""
        return self.contact_email or self.from_email


_settings: Settings | None = None


def settings() -> Settings:
    """Construct settings lazily so importing never requires env vars."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
