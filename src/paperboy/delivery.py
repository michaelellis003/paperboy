"""Deliver documents to an e-reader through pluggable backends.

Backends by device market share:

- ``email`` — Kindle's Send-to-Kindle address, PocketBook's
  Send-to-PocketBook, and any other vendor that accepts documents by
  email. Constraints follow Amazon's (the strictest common case): max
  25 attachments per email, 50 MB total, sender must be approved.
- ``dropbox`` — Kobo devices natively sync a linked Dropbox folder;
  uploads land there and appear on the device.

reMarkable (real cloud API) is on the roadmap.
"""

import json
import smtplib
from email.message import EmailMessage

import httpx

from .config import Settings, settings
from .net import client

MAX_ATTACHMENTS = 25
MAX_TOTAL_BYTES = 50 * 1024 * 1024
# Target size when auto-splitting batches: Gmail (the common SMTP
# provider) caps outgoing messages at ~25 MB even though Amazon
# accepts 50 MB, so chunk to the stricter bound.
CHUNK_TARGET_BYTES = 25 * 1024 * 1024

_MIME_BY_EXTENSION = {
    ".pdf": ("application", "pdf"),
    ".epub": ("application", "epub+zip"),
    ".docx": (
        "application",
        "vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    ".html": ("text", "html"),
}

_DROPBOX_TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
_DROPBOX_UPLOAD_URL = "https://content.dropboxapi.com/2/files/upload"


class DeliveryError(Exception):
    """Raised when documents cannot be delivered to the device."""


def _manifest(documents: list[tuple[str, bytes]]) -> str:
    """Per-file listing with sizes, for receipts."""
    return ", ".join(
        f"{name} ({len(content) / 1e6:.1f} MB)" for name, content in documents
    )


def send_documents(documents: list[tuple[str, bytes]]) -> str:
    """Deliver documents to the configured e-reader.

    ``documents`` is a list of (filename, content) pairs. Returns a
    short human-readable receipt. Raises DeliveryError on constraint
    violations or missing configuration.
    """
    if not documents:
        raise DeliveryError("No documents to send.")
    cfg = settings()
    if cfg.delivery_method == "email":
        return _send_email(cfg, documents)
    if cfg.delivery_method == "dropbox":
        return _send_dropbox(cfg, documents)
    raise DeliveryError(
        f"Unknown DELIVERY_METHOD {cfg.delivery_method!r}; "
        "expected 'email' or 'dropbox'."
    )


def _require(cfg: Settings, names: list[str]) -> None:
    missing = [name for name in names if not getattr(cfg, name.lower(), "")]
    if missing:
        raise DeliveryError(
            "Delivery is not configured. Missing: " + ", ".join(missing)
        )


def _send_email(cfg: Settings, documents: list[tuple[str, bytes]]) -> str:
    _require(
        cfg,
        [
            "DEVICE_EMAIL",
            "SMTP_HOST",
            "SMTP_USER",
            "SMTP_PASSWORD",
            "FROM_EMAIL",
        ],
    )
    if len(documents) > MAX_ATTACHMENTS:
        raise DeliveryError(
            f"Email delivery accepts at most {MAX_ATTACHMENTS} attachments "
            f"per message; got {len(documents)}. Split into multiple sends."
        )
    total = sum(len(content) for _, content in documents)
    if total > MAX_TOTAL_BYTES:
        if len(documents) == 1:
            raise DeliveryError(
                f"{documents[0][0]} is {total / 1e6:.1f} MB, over the "
                "50 MB per-email limit — too large for email delivery."
            )
        raise DeliveryError(
            f"Attachments total {total / 1e6:.1f} MB, over the 50 MB "
            "per-email limit. Split into multiple sends."
        )

    msg = EmailMessage()
    msg["From"] = cfg.from_email
    msg["To"] = cfg.device_email
    msg["Subject"] = "Papers from paperboy"
    msg.set_content("Delivered by paperboy.")

    for filename, content in documents:
        ext = (
            "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        )
        maintype, subtype = _MIME_BY_EXTENSION.get(
            ext, ("application", "octet-stream")
        )
        msg.add_attachment(
            content, maintype=maintype, subtype=subtype, filename=filename
        )

    try:
        if cfg.smtp_port == 465:
            smtp: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port)
        else:
            smtp = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port)
            smtp.starttls()
        try:
            smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        finally:
            smtp.quit()
    except (OSError, smtplib.SMTPException) as exc:
        raise DeliveryError(
            f"SMTP delivery failed ({type(exc).__name__}): {exc}"
        ) from exc

    return (
        f"Sent {len(documents)} document(s), {total / 1e6:.1f} MB total, "
        f"to {cfg.device_email}: {_manifest(documents)}"
    )


def _dropbox_access_token(cfg: Settings) -> str:
    try:
        response = client.post(
            _DROPBOX_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": cfg.dropbox_refresh_token,
                "client_id": cfg.dropbox_app_key,
                "client_secret": cfg.dropbox_app_secret,
            },
        )
    except httpx.HTTPError as exc:
        raise DeliveryError(
            f"Dropbox unreachable ({type(exc).__name__}): {exc}"
        ) from exc
    if response.status_code != 200:
        raise DeliveryError(
            f"Dropbox token refresh failed ({response.status_code}): "
            f"{response.text[:200]}"
        )
    return response.json()["access_token"]


def _send_dropbox(cfg: Settings, documents: list[tuple[str, bytes]]) -> str:
    _require(
        cfg,
        ["DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"],
    )
    total = sum(len(content) for _, content in documents)
    if total > MAX_TOTAL_BYTES:
        raise DeliveryError(
            f"Batch totals {total / 1e6:.1f} MB, over the "
            f"{MAX_TOTAL_BYTES / 1e6:.0f} MB per-batch cap. "
            "Split into multiple sends."
        )
    token = _dropbox_access_token(cfg)
    folder = cfg.dropbox_folder.rstrip("/")
    for filename, content in documents:
        api_arg = json.dumps(
            {"path": f"{folder}/{filename}", "mode": "overwrite", "mute": True}
        )
        try:
            response = client.post(
                _DROPBOX_UPLOAD_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Dropbox-API-Arg": api_arg,
                    "Content-Type": "application/octet-stream",
                },
                content=content,
            )
        except httpx.HTTPError as exc:
            raise DeliveryError(
                f"Dropbox upload of {filename!r} failed "
                f"({type(exc).__name__}): {exc}"
            ) from exc
        if response.status_code != 200:
            raise DeliveryError(
                f"Dropbox upload of {filename!r} failed "
                f"({response.status_code}): {response.text[:200]}"
            )

    return (
        f"Uploaded {len(documents)} document(s), {total / 1e6:.1f} MB total, "
        f"to Dropbox folder '{folder}' (same-name files are overwritten): "
        f"{_manifest(documents)}"
    )
