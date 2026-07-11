import json
import smtplib
from typing import ClassVar

import httpx
import pytest

from paperboy import delivery

DOC = ("paper.pdf", b"%PDF-1.4")


class FakeSMTP:
    instances: ClassVar[list] = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.tls = False
        self.creds = None
        self.messages = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self):
        self.tls = True

    def login(self, user, password):
        self.creds = (user, password)

    def send_message(self, msg):
        self.messages.append(msg)

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.instances = []


def test_empty_documents_rejected(env):
    with pytest.raises(delivery.DeliveryError, match="No documents"):
        delivery.send_documents([])


def test_unknown_method_rejected(env):
    env.setenv("DELIVERY_METHOD", "carrier-pigeon")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    with pytest.raises(delivery.DeliveryError, match="carrier-pigeon"):
        delivery.send_documents([DOC])


def test_email_missing_config_lists_missing(env):
    env.setenv("DEVICE_EMAIL", "")
    env.setenv("KINDLE_EMAIL", "")
    env.setenv("SMTP_HOST", "")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    with pytest.raises(delivery.DeliveryError, match="DEVICE_EMAIL, SMTP_HOST"):
        delivery.send_documents([DOC])


def test_too_many_attachments(env):
    docs = [(f"p{i}.pdf", b"x") for i in range(26)]
    with pytest.raises(delivery.DeliveryError, match="at most 25"):
        delivery.send_documents(docs)


def test_too_large(env):
    docs = [("big.pdf", b"x" * (delivery.MAX_TOTAL_BYTES + 1))]
    with pytest.raises(delivery.DeliveryError, match="50 MB"):
        delivery.send_documents(docs)


def test_email_send_ssl(env, monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    receipt = delivery.send_documents([DOC, ("book.epub", b"epub")])
    smtp = FakeSMTP.instances[0]
    assert smtp.creds == ("user@example.com", "hunter2")
    assert smtp.quit_called
    msg = smtp.messages[0]
    assert msg["To"] == "reader@kindle.com"
    attachments = list(msg.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["paper.pdf", "book.epub"]
    assert attachments[0].get_content_type() == "application/pdf"
    assert attachments[1].get_content_type() == "application/epub+zip"
    assert "2 document(s)" in receipt


def test_email_send_starttls(env, monkeypatch):
    env.setenv("SMTP_PORT", "587")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    delivery.send_documents([DOC])
    assert FakeSMTP.instances[0].tls is True


@pytest.fixture
def dropbox_env(env):
    env.setenv("DELIVERY_METHOD", "dropbox")
    env.setenv("DROPBOX_APP_KEY", "key")
    env.setenv("DROPBOX_APP_SECRET", "secret")
    env.setenv("DROPBOX_REFRESH_TOKEN", "refresh")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    return env


def _dropbox_client(uploads, token_status=200, upload_status=200):
    def handler(request):
        if request.url.host == "api.dropboxapi.com":
            return httpx.Response(token_status, json={"access_token": "tok"})
        assert request.headers["Authorization"] == "Bearer tok"
        uploads.append(json.loads(request.headers["Dropbox-API-Arg"])["path"])
        return httpx.Response(upload_status, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_dropbox_upload(dropbox_env, monkeypatch):
    uploads = []
    monkeypatch.setattr(delivery, "client", _dropbox_client(uploads))
    receipt = delivery.send_documents([DOC])
    assert uploads == ["/Books/paper.pdf"]
    assert "Dropbox folder '/Books'" in receipt


def test_dropbox_missing_config(dropbox_env):
    dropbox_env.setenv("DROPBOX_REFRESH_TOKEN", "")
    import paperboy.config

    dropbox_env.setattr(paperboy.config, "_settings", None)
    with pytest.raises(delivery.DeliveryError, match="DROPBOX_REFRESH_TOKEN"):
        delivery.send_documents([DOC])


def test_dropbox_token_failure(dropbox_env, monkeypatch):
    monkeypatch.setattr(
        delivery, "client", _dropbox_client([], token_status=400)
    )
    with pytest.raises(delivery.DeliveryError, match="token refresh failed"):
        delivery.send_documents([DOC])


def test_dropbox_upload_failure(dropbox_env, monkeypatch):
    monkeypatch.setattr(
        delivery, "client", _dropbox_client([], upload_status=409)
    )
    with pytest.raises(delivery.DeliveryError, match=r"upload of 'paper\.pdf'"):
        delivery.send_documents([DOC])
