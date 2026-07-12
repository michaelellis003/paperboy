"""Tests for the Google OAuth layer on the HTTP transport."""

import pytest
from fastmcp.server.auth.auth import AccessToken

from paperboy import oauth

STATIC = "s" * 40

OAUTH_ENV = {
    "GOOGLE_OAUTH_CLIENT_ID": "123.apps.googleusercontent.com",
    "GOOGLE_OAUTH_CLIENT_SECRET": "GOCSPX-test",
    "SERVER_BASE_URL": "https://paperboy.example.com",
    "OAUTH_ALLOWED_EMAILS": "owner@example.com",
}


def _google_token(email, verified=True):
    return AccessToken(
        token="google-token",
        client_id="google-sub",
        scopes=["openid", "email"],
        claims={"email": email, "email_verified": verified},
    )


@pytest.fixture
def oauth_env(monkeypatch):
    for key, value in OAUTH_ENV.items():
        monkeypatch.setenv(key, value)


def test_oauth_configured_requires_all_three(monkeypatch, oauth_env):
    assert oauth.oauth_configured()
    monkeypatch.delenv("SERVER_BASE_URL")
    assert not oauth.oauth_configured()


def test_allowed_emails_parses_and_normalizes(monkeypatch):
    monkeypatch.setenv(
        "OAUTH_ALLOWED_EMAILS", " Owner@Example.com, second@x.org ,"
    )
    assert oauth.allowed_emails() == {"owner@example.com", "second@x.org"}


def test_build_auth_refuses_empty_allowlist(monkeypatch, oauth_env):
    monkeypatch.setenv("OAUTH_ALLOWED_EMAILS", "")
    with pytest.raises(SystemExit, match="OAUTH_ALLOWED_EMAILS"):
        oauth.build_auth(static_token=STATIC)


async def _verify(provider, monkeypatch, google_result):
    async def fake_google_verify(self, token):
        return google_result

    monkeypatch.setattr(
        oauth.GoogleTokenVerifier, "verify_token", fake_google_verify
    )
    return await provider._token_validator.verify_token("google-token")


@pytest.mark.anyio
async def test_owner_email_is_accepted(monkeypatch, oauth_env):
    provider = oauth.build_auth(static_token=STATIC)
    result = await _verify(
        provider, monkeypatch, _google_token("Owner@Example.com")
    )
    assert result is not None


@pytest.mark.anyio
async def test_stranger_email_is_rejected(monkeypatch, oauth_env):
    provider = oauth.build_auth(static_token=STATIC)
    result = await _verify(
        provider, monkeypatch, _google_token("intruder@gmail.com")
    )
    assert result is None


@pytest.mark.anyio
async def test_unverified_owner_email_is_rejected(monkeypatch, oauth_env):
    provider = oauth.build_auth(static_token=STATIC)
    result = await _verify(
        provider,
        monkeypatch,
        _google_token("owner@example.com", verified=False),
    )
    assert result is None


@pytest.mark.anyio
async def test_google_rejection_passes_through(monkeypatch, oauth_env):
    provider = oauth.build_auth(static_token=STATIC)
    result = await _verify(provider, monkeypatch, None)
    assert result is None


@pytest.mark.anyio
async def test_static_token_still_works(oauth_env):
    provider = oauth.build_auth(static_token=STATIC)
    result = await provider.verify_token(STATIC)
    assert result is not None
    assert result.client_id == "paperboy-owner"


@pytest.mark.anyio
async def test_wrong_static_token_falls_through_to_oauth(
    monkeypatch, oauth_env
):
    provider = oauth.build_auth(static_token=STATIC)

    async def fake_super_verify(self, token):
        return None

    monkeypatch.setattr(oauth.GoogleProvider, "verify_token", fake_super_verify)
    assert await provider.verify_token("t" * 40) is None
