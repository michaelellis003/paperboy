"""Google OAuth for the HTTP transport, for clients without header support.

claude.ai and the mobile app can only authenticate custom connectors
through an OAuth flow (their connector dialog has no bearer-token field
on most accounts). FastMCP's GoogleProvider handles that flow: it
proxies claude.ai's client registration and login redirect to Google,
then issues its own session tokens.

Two paperboy-specific rules on top:

- Only the owner may authorize. Google would happily authenticate any
  Google account; ``OAUTH_ALLOWED_EMAILS`` closes that to the addresses
  listed there. This server sends email as the owner, so this check is
  load-bearing, not cosmetic.
- The static MCP_AUTH_TOKEN keeps working alongside OAuth, so Claude
  Code and API clients configured with the bearer header are unaffected.
"""

import logging
import os
import secrets

from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import (
    GoogleProvider,
    GoogleTokenVerifier,
)

logger = logging.getLogger(__name__)

_EMAIL_SCOPES = ["openid", "email"]


def allowed_emails() -> set[str]:
    """Parse OAUTH_ALLOWED_EMAILS into a normalized set."""
    raw = os.environ.get("OAUTH_ALLOWED_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


class OwnerOnlyVerifier(GoogleTokenVerifier):
    """Google token verifier that rejects everyone but the owner.

    Runs after Google has validated the token: the account must have a
    verified email on the allowlist, or authorization fails before any
    session is issued.
    """

    def __init__(self, allowed: set[str], **kwargs) -> None:
        super().__init__(**kwargs)
        self._allowed = allowed

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify with Google, then require an allowlisted owner email."""
        result = await super().verify_token(token)
        if result is None:
            return None
        email = (result.claims.get("email") or "").strip().lower()
        verified = result.claims.get("email_verified") in (True, "true", "1")
        if verified and email in self._allowed:
            return result
        logger.warning(
            "OAuth sign-in rejected for %r (verified=%s): not on "
            "OAUTH_ALLOWED_EMAILS",
            email or "<no email>",
            verified,
        )
        return None


class PaperboyAuth(GoogleProvider):
    """GoogleProvider that also accepts the static bearer token.

    verify_token sees every authenticated request, whichever way the
    client signed in, so checking the static token here keeps one auth
    stack for both kinds of clients.
    """

    def __init__(self, *, static_token: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._static_token = static_token
        # GoogleProvider builds its own verifier; swap in the
        # owner-restricted one with the same scope requirements.
        self._token_validator = OwnerOnlyVerifier(
            allowed=allowed_emails(),
            required_scopes=self._token_validator.required_scopes,
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        """Accept the static bearer token, else defer to the OAuth flow."""
        if self._static_token and secrets.compare_digest(
            token, self._static_token
        ):
            return AccessToken(
                token=token, client_id="paperboy-owner", scopes=[]
            )
        return await super().verify_token(token)


def oauth_configured() -> bool:
    """Whether the three OAuth variables are all present."""
    return all(
        os.environ.get(name)
        for name in (
            "GOOGLE_OAUTH_CLIENT_ID",
            "GOOGLE_OAUTH_CLIENT_SECRET",
            "SERVER_BASE_URL",
        )
    )


def build_auth(static_token: str) -> PaperboyAuth:
    """Build the OAuth provider from the environment.

    Requires GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, and
    SERVER_BASE_URL (the public https URL of this deployment), plus a
    non-empty OAUTH_ALLOWED_EMAILS — refusing to start beats silently
    serving an open OAuth endpoint.
    """
    if not allowed_emails():
        raise SystemExit(
            "OAuth is configured but OAUTH_ALLOWED_EMAILS is empty. "
            "Set it to the Google account email(s) allowed to sign in "
            "(normally just your own)."
        )
    return PaperboyAuth(
        static_token=static_token,
        client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
        base_url=os.environ["SERVER_BASE_URL"],
        required_scopes=_EMAIL_SCOPES,
    )
