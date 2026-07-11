import builtins
import getpass
import smtplib

import httpx
import pytest

from paperboy import setup_wizard

# --- .env writing ---------------------------------------------------------


def test_quote_plain_and_spaced():
    assert setup_wizard.quote("plain") == "plain"
    assert setup_wizard.quote("has space") == '"has space"'


def test_update_env_creates_file(tmp_path):
    path = tmp_path / ".env"
    setup_wizard.update_env({"A": "1", "B": "two words"}, path=str(path))
    assert path.read_text() == 'A=1\nB="two words"\n'
    assert (path.stat().st_mode & 0o777) == 0o600


def test_run_aborts_cleanly_on_eof(tmp_path, monkeypatch, capsys):
    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", eof)
    setup_wizard.run(["--env", str(tmp_path / ".env")])
    assert "Setup aborted" in capsys.readouterr().out
    assert not (tmp_path / ".env").exists()


def test_update_env_preserves_comments_and_updates(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# comment\nA=old\nC=keep\n")
    setup_wizard.update_env({"A": "new", "B": "added"}, path=str(path))
    content = path.read_text()
    assert "# comment" in content
    assert "A=new" in content and "A=old" not in content
    assert "C=keep" in content
    assert "B=added" in content


# --- validators -----------------------------------------------------------


class FakeSMTP:
    fail = False

    def __init__(self, host, port, timeout=None):
        pass

    def starttls(self):
        pass

    def login(self, user, password):
        if FakeSMTP.fail:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def quit(self):
        pass


@pytest.fixture(autouse=True)
def reset_fake_smtp():
    FakeSMTP.fail = False


def test_validate_smtp_ok(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    assert setup_wizard.validate_smtp("h", 465, "u", "p") is None


def test_validate_smtp_starttls_failure(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    FakeSMTP.fail = True
    error = setup_wizard.validate_smtp("h", 587, "u", "p")
    assert error is not None and "SMTPAuthenticationError" in error


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_validate_zotero_key_ok(monkeypatch):
    payload = {
        "userID": 42,
        "access": {"user": {"library": True, "write": True}},
    }
    monkeypatch.setattr(
        setup_wizard,
        "client",
        _client(lambda request: httpx.Response(200, json=payload)),
    )
    assert setup_wizard.validate_zotero_key("k") == ("42", None)


def test_validate_zotero_key_invalid(monkeypatch):
    monkeypatch.setattr(
        setup_wizard,
        "client",
        _client(lambda request: httpx.Response(403, text="Invalid key")),
    )
    user_id, error = setup_wizard.validate_zotero_key("bad")
    assert user_id is None and error is not None and "403" in error


def test_validate_zotero_key_missing_write(monkeypatch):
    payload = {"userID": 42, "access": {"user": {"library": True}}}
    monkeypatch.setattr(
        setup_wizard,
        "client",
        _client(lambda request: httpx.Response(200, json=payload)),
    )
    user_id, error = setup_wizard.validate_zotero_key("k")
    assert user_id is None and error is not None and "write" in error


def test_dropbox_authorize_url():
    url = setup_wizard.dropbox_authorize_url("KEY123")
    assert "client_id=KEY123" in url and "token_access_type=offline" in url


def test_exchange_dropbox_code_ok(monkeypatch):
    monkeypatch.setattr(
        setup_wizard,
        "client",
        _client(
            lambda request: httpx.Response(200, json={"refresh_token": "RT"})
        ),
    )
    assert setup_wizard.exchange_dropbox_code("k", "s", "code") == ("RT", None)


def test_exchange_dropbox_code_rejected(monkeypatch):
    monkeypatch.setattr(
        setup_wizard,
        "client",
        _client(lambda request: httpx.Response(400, text="invalid_grant")),
    )
    token, error = setup_wizard.exchange_dropbox_code("k", "s", "bad")
    assert token is None and error is not None and "invalid_grant" in error


# --- scripted end-to-end run (Kindle flow) --------------------------------


def test_run_kindle_flow(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    answers = iter(
        [
            "1",  # device: Kindle
            "n",  # open Amazon page in browser?
            "me_123@kindle.com",  # send-to-kindle address
            "me@example.com",  # sender
            "n",  # open app-passwords page?
            "smtp.example.com",  # SMTP host
            "465x",  # SMTP port: non-numeric, re-prompted
            "465",  # SMTP port
            "me@example.com",  # SMTP user
            "y",  # set up Zotero?
            "n",  # open Zotero keys page?
            "y",  # remote use? -> generate token
        ]
    )
    secrets_answers = iter(["app-password", "zoterokey123"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt="": next(secrets_answers)
    )
    monkeypatch.setattr(setup_wizard, "validate_smtp", lambda *args: None)
    monkeypatch.setattr(
        setup_wizard, "validate_zotero_key", lambda key: ("77", None)
    )

    setup_wizard.run(["--env", str(env_path)])

    content = env_path.read_text()
    assert "DELIVERY_METHOD=email" in content
    assert "DEVICE_EMAIL=me_123@kindle.com" in content
    assert "SMTP_PASSWORD=app-password" in content
    assert "ZOTERO_API_KEY=zoterokey123" in content
    assert "ZOTERO_LIBRARY_ID=77" in content
    assert "MCP_AUTH_TOKEN=" in content


def test_run_kobo_flow(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    answers = iter(
        [
            "2",  # device: Kobo
            "n",  # open Dropbox apps page?
            "APPKEY",  # app key
            "n",  # open authorize URL?
            "authcode",  # OAuth code
            "",  # contact email: empty is rejected, re-prompted
            "kobo@example.com",  # contact email for Unpaywall
            "n",  # Zotero?
            "n",  # remote?
        ]
    )
    secrets_answers = iter(["APPSECRET"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": next(answers))
    monkeypatch.setattr(
        getpass, "getpass", lambda prompt="": next(secrets_answers)
    )
    monkeypatch.setattr(
        setup_wizard,
        "exchange_dropbox_code",
        lambda key, secret, code: ("REFRESH", None),
    )

    setup_wizard.run(["--env", str(env_path)])

    content = env_path.read_text()
    assert "DELIVERY_METHOD=dropbox" in content
    assert 'DROPBOX_FOLDER="/Apps/Rakuten Kobo"' in content
    assert "DROPBOX_REFRESH_TOKEN=REFRESH" in content
    assert "CONTACT_EMAIL=kobo@example.com" in content
