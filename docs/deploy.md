# Remote deployment (Cloud Run)

Local use needs none of this. Deploy to Cloud Run when you want paperboy
from claude.ai, your phone, or any machine that isn't running the server.

Every deployment is single-tenant. You run your own instance with your
own secrets and token, and pay your own bill, which normally stays at
$0. To run yours:

```bash
uv run paperboy setup          # collect credentials into .env
gcloud auth login
./deploy/deploy.sh my-paperboy-project
```

The script creates a dedicated GCP project, stores secrets in Secret
Manager, and runs the service under a service account whose only
permission is reading those secrets. Cost is bounded several ways:
`--max-instances=1`, scale-to-zero, the `us-central1` free-tier region
(2M requests/month, shared across your billing account), and a $1/month
budget with 50%/100% alert thresholds that email your billing admins.
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

Google has no API for external OAuth consent screens, so steps 1–2
are one-time console clicks (`deploy.sh` prints these same steps with
your project's exact URLs and redirect URI filled in):

1. Open `https://console.cloud.google.com/auth/overview?project=<your
   project id>`. Choose External, fill in the app name and your email,
   and add yourself as a test user.
2. Create an OAuth client at `https://console.cloud.google.com/auth/
   clients/create?project=<your project id>`: type Web application,
   with an authorized redirect URI of exactly
   `<your service URL>/auth/callback` (no `/mcp`, no trailing slash),
   for example
   `https://paperboy-xxxx.us-central1.run.app/auth/callback`.
3. Add to `.env`:

   ```bash
   GOOGLE_OAUTH_CLIENT_ID=<client id>.apps.googleusercontent.com
   GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
   SERVER_BASE_URL=https://<your service URL>       # no /mcp suffix
   OAUTH_ALLOWED_EMAILS=you@gmail.com
   ```

4. Re-run `./deploy/deploy.sh <project>` to sync and redeploy.
5. On claude.ai: Settings > Connectors > Add custom connector. Name it,
   paste `<your service URL>/mcp`, leave the OAuth fields empty, and
   Add. You'll be sent through Google sign-in once; mobile picks the
   connector up automatically.

Only accounts on `OAUTH_ALLOWED_EMAILS` can complete sign-in, and
anyone on that list has full control of the server — sending email
from your address and editing your Zotero library — so it is normally
just your own address. While the consent screen is in Testing mode,
Google expires sign-ins after 7 days (you'll be asked to sign in
again); publishing the app removes that limit but shows an
"unverified app" warning during sign-in, which is fine when the only
user is you.

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
