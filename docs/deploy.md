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

Clients differ in whether they accept the static bearer token, so
check your target client before deploying:

| Client | Works today? |
|---|---|
| Claude Code | Yes: `claude mcp add --transport http paperboy <URL>/mcp --header "Authorization: Bearer <token>"` |
| Claude API (MCP connector) | Yes: `authorization_token` parameter |
| claude.ai & mobile (any plan) | Check your Add-connector dialog. Anthropic is rolling out a beta "Request headers" section that accepts bearer tokens ([docs](https://claude.com/docs/connectors/custom/remote-mcp)); if your dialog shows it, paste `Authorization: Bearer <token>` there. If it shows only URL + OAuth client fields, you need the OAuth path: FastMCP ships provider integrations (Google, GitHub, Auth0, ...) that slot into `mcp.auth`. On the roadmap |

Client support changes often. Your own connector dialog is the
authority, not this table.

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
