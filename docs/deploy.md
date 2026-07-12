# Remote deployment (Cloud Run)

Local use needs none of this. Deploy to Cloud Run when you want paperboy
from claude.ai, your phone, or any machine that isn't running the server.

Every deployment is single-tenant. You run your own instance with your
own secrets and token, and pay your own bill, which normally stays at
$0.

## Before you start

You need three things:

- **The gcloud CLI**, installed and signed in. On macOS the documented
  install is the archive plus `./google-cloud-sdk/install.sh`, then
  `gcloud init`, which signs you in as part of first-run setup
  ([install guide](https://cloud.google.com/sdk/docs/install)). If you
  installed gcloud a while ago, `gcloud auth login` refreshes your
  credentials.
- **A Google Cloud account with billing enabled.** Signing up gives you
  a $300 welcome credit over 90 days, and a payment method is required
  even to stay within the free tier. Cloud Run bills nothing at
  paperboy's traffic, but Google still wants a card on file. You are
  not charged unless you manually upgrade to a paid account
  ([free tier](https://cloud.google.com/free),
  [billing](https://cloud.google.com/billing/docs/how-to/manage-billing-account)).
- **Your credentials in `.env`**, from `uv run paperboy setup`.

Then, from the repo directory:

```bash
./deploy/deploy.sh my-paperboy-project
```

The script creates a dedicated GCP project, enables the APIs it needs
(Cloud Run, Cloud Build, Artifact Registry, Secret Manager), stores
secrets in Secret Manager, and runs the service under a service account
whose only permission is reading those secrets. Cost is bounded several
ways: `--max-instances=1`, scale-to-zero, the `us-central1` free-tier
region, and a $1/month budget with 50%/100% alert thresholds that email
your billing admins. Cloud Run's Always Free allowance is 2 million
requests, 180,000 vCPU-seconds, and 360,000 GiB-seconds per month,
shared across your billing account; scale-to-zero means an idle service
costs nothing ([Cloud Run pricing](https://cloud.google.com/run/pricing)).
Re-running the script is a clean sync; rotated secrets are picked up
and nothing is duplicated. A cleanup policy keeps only the two newest
container images so Artifact Registry storage stays inside the free
tier.

Two details you may notice when auditing your own project. First,
`gcloud run services describe` can show a service-level "Max: 20";
that is a platform default, and the deployed revision's max-instances=1
governs because the effective limit is the lesser of the two. Second,
rotating a secret leaves the old version enabled in Secret Manager.
Disable superseded versions if you rotate often; past 6 active versions
Secret Manager bills about $0.06 per version per month.

## Security model

When `PORT` is set (Cloud Run sets it), paperboy serves Streamable HTTP
at `/mcp` and requires `MCP_AUTH_TOKEN`. It refuses to start
unauthenticated because the server can send email as you. Requests
without the token get a 401 before any tool logic runs. Ingress is
public because Claude clients can't send Google IAM tokens; the bearer
token is the gate. Note that the budget emails you at its thresholds
but does not cap billing, so act on the 50% alert if it ever fires.

## Connecting clients to the remote server

| Client | How |
|---|---|
| Claude Code | `claude mcp add --transport http paperboy <URL>/mcp --header "Authorization: Bearer <token>"` |
| Claude API (MCP connector) | `authorization_token` parameter |
| claude.ai & mobile | Two paths. If your Add-connector dialog has a "Request headers" section (a beta Anthropic is rolling out, [docs](https://claude.com/docs/connectors/custom/remote-mcp)), paste `Authorization: Bearer <token>` there. Otherwise set up Google OAuth (below), then add the connector with just the URL — leave the dialog's OAuth client fields empty; Claude discovers the flow from the server and sends you through a Google sign-in |

Client support changes often. Your own connector dialog is the
authority, not this table.

## Google OAuth for claude.ai and mobile

The bearer token covers Claude Code and the API, but most claude.ai
accounts can only authenticate custom connectors through OAuth. Four
variables in `.env` switch that on; the bearer token keeps working
alongside it.

Google has no API for this, so steps 1–2 are one-time clicks in
Google's console. Google now groups them under "Google Auth Platform";
`deploy.sh` prints these same steps with your project's exact URLs and
redirect URI already filled in:

1. Open `https://console.cloud.google.com/auth/overview?project=<your
   project id>` and click **Get started**. Set an app name and your
   email, choose **External** for the audience, and add your own Google
   address as a test user. External here just means "any Google
   account can be a user"; the allowlist below is what actually limits
   access.
2. Under **Clients**, click **Create client**, choose **Web
   application**, and add an authorized redirect URI of exactly
   `<your service URL>/auth/callback` (no `/mcp`, no trailing slash),
   for example
   `https://paperboy-xxxx.us-central1.run.app/auth/callback`. Redirect
   URI changes can take a few minutes to a few hours to take effect, so
   if sign-in fails right after this, wait and retry before assuming
   something is wrong
   ([Google's OAuth client docs](https://support.google.com/cloud/answer/15549257)).
3. Add to `.env`:

   ```bash
   GOOGLE_OAUTH_CLIENT_ID=<client id>.apps.googleusercontent.com
   GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
   # SERVER_BASE_URL is your service URL, the same one from the
   # redirect URI above, without the /auth/callback or /mcp suffix
   SERVER_BASE_URL=https://<your service URL>
   OAUTH_ALLOWED_EMAILS=you@gmail.com
   ```

4. Re-run `./deploy/deploy.sh <project>` to sync and redeploy.
5. On claude.ai: Settings > Connectors > Add custom connector. Name it,
   paste `<your service URL>/mcp`, leave the OAuth fields empty, and
   Add. You'll be sent through Google sign-in once; mobile picks the
   connector up automatically.

Two things worth knowing about this setup:

- **Only accounts on `OAUTH_ALLOWED_EMAILS` can sign in**, and anyone
  on that list has full control of the server (sending email from your
  address and editing your Zotero library), so it is normally just your
  own address.
- **You can leave the app in Testing; your sign-in will not expire.**
  Google normally expires test-user sign-ins after 7 days, but it makes
  an exception when an app requests only basic profile scopes, and
  paperboy requests only `openid` and `email`. So there is no need to
  publish the app, and no "unverified app" warning to click through
  ([Google's OAuth2 docs, refresh-token expiration](https://developers.google.com/identity/protocols/oauth2)).

## How credentials flow

The wizard runs once; everything else reads its output:

```
paperboy setup ──► .env (single source of truth, chmod 600)
                    ├──► local server: re-read at every startup
                    │    (spawned per session over stdio — not a daemon)
                    └──► deploy.sh: copied into Secret Manager at
                         deploy time; the Cloud Run instance never
                         sees .env and runs independently afterward
```

Local and cloud instances can run at the same time without conflict.
The server is stateless, so all state (queue, sent-tags, collections)
lives in Zotero and both instances see the same truth. A paper sent
from your phone shows as already sent in your laptop session.

One thing to remember: after editing `.env` (say, rotating a
password), the local server picks it up at its next start, but the
cloud instance keeps its Secret Manager copy until you re-run
`deploy/deploy.sh`, which syncs the new values and redeploys.
