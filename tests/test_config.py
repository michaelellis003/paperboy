import os

import paperboy.config
from paperboy.config import Settings


def test_load_env_file_fills_missing_vars(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        'DEVICE_EMAIL=file@kindle.com\nSMTP_HOST="from file"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEVICE_EMAIL", raising=False)
    monkeypatch.setenv("SMTP_HOST", "already-set")
    paperboy.config.load_env_file()
    assert os.environ["DEVICE_EMAIL"] == "file@kindle.com"
    assert os.environ["SMTP_HOST"] == "already-set"


def test_load_env_file_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    paperboy.config.load_env_file()


def test_kindle_email_alias(env):
    env.setenv("DEVICE_EMAIL", "")
    env.setenv("KINDLE_EMAIL", "legacy@kindle.com")
    assert Settings().device_email == "legacy@kindle.com"


def test_device_email_wins_over_alias(env):
    env.setenv("KINDLE_EMAIL", "legacy@kindle.com")
    assert Settings().device_email == "reader@kindle.com"


def test_zotero_disabled_by_default(env):
    assert Settings().zotero_enabled is False


def test_zotero_enabled(env):
    env.setenv("ZOTERO_API_KEY", "k")
    env.setenv("ZOTERO_LIBRARY_ID", "123")
    assert Settings().zotero_enabled is True


def test_polite_email_falls_back_to_from_email(env):
    assert Settings().polite_email == "user@example.com"
    env.setenv("CONTACT_EMAIL", "polite@example.com")
    assert Settings().polite_email == "polite@example.com"


def test_settings_cached(env):
    assert paperboy.config.settings() is paperboy.config.settings()
