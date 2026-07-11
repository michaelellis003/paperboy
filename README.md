# paperboy

An MCP server that delivers research papers to your e-reader, with Zotero
as the source of truth. Ask Claude for a reading list, then say "queue
them and send to my Kindle." (MCP is the plugin protocol Claude uses:
this program runs on your machine or your cloud project, and Claude
calls its tools during conversation.) Works locally in Claude Code and
Claude Desktop today. Remote use via Cloud Run works from Claude Code,
the API, and wherever claude.ai's connector dialog supports bearer
tokens (see "Connecting clients" below).

Zotero itself is optional: without it you can still search and send
papers one-off. The reading queue, collections, and duplicate
protection across sessions need it.

## Architecture

Hub-and-spoke. Zotero is the library hub and this server is the delivery
spoke, so all state lives in Zotero:

- Papers land in a **Reading Queue** collection, created on demand.
- Delivery is recorded by tagging the item `sent-to-ereader`.
- Papers can also be filed into topical collections. Zotero items can
  belong to many collections at once, so filing never disturbs queue
  state. Claude proposes a collection based on the paper's topic and
  your existing collection names, and asks you when the fit is unclear.
- The server itself is stateless. No database, safe to redeploy.

Papers are resolved by arXiv id (arXiv Atom API), DOI (Crossref metadata
plus Unpaywall open-access PDF lookup), or bare title (OpenAlex search,
accepted only on a high-confidence fuzzy match, so a reading list Claude
produced in conversation can be sent directly). Search runs against
OpenAlex (~250M works) or arXiv. When a paper can't be resolved or has
no open-access PDF, the tool says so in its receipt and Claude relays
that to you. Nothing is dropped silently.

### Delivery backends

| Backend | Devices | How |
|---|---|---|
| `email` (default) | Kindle, PocketBook, anything with an email intake | SMTP to the device address. Kindle constraints enforced: 25 attachments / 50 MB per email; sender must be on the Approved Personal Document E-mail List |
| `dropbox` | Kobo (native Dropbox sync on the device) | Uploads via the Dropbox API. Kobo only syncs `Apps/Rakuten Kobo/`, so use a **Full Dropbox**-scoped app with `DROPBOX_FOLDER="/Apps/Rakuten Kobo"`; an App-folder-scoped app can't reach it |

reMarkable (real cloud API) is on the roadmap.

## Tools

| Tool | What it does |
|---|---|
| `search_papers` | Search OpenAlex (general) or arXiv (`source="arxiv"`); results carry a `ref` and an `open_access_pdf` flag |
| `recommend_papers` | Discover related or new papers: citation-graph recommendations (Semantic Scholar) seeded from your Zotero library, plus keyword discovery from interests Claude distills out of the conversation. Excludes papers you already have |
| `send_papers` | One-off send by arXiv id, DOI, URL, or title (also records in Zotero if configured) |
| `queue_papers` | Add papers to the Zotero Reading Queue without sending (optionally filed into topical collections) |
| `list_collections` | List Zotero collections so Claude can propose where to file a paper, or ask you |
| `file_papers` | File queued papers into a topical collection (created on demand; queue membership unaffected) |
| `list_queue` | Show the queue with per-item status (unsent / sent / no-open-access-pdf) |
| `remove_from_queue` | Delete queue items by exact ref or title |
| `send_queue` | Send every unsent queue item (auto-split under email limits), then tag as sent |
| `setup_status` | Report what's configured and what's missing (no secrets) so Claude can guide setup |

## Setup

Run the interactive wizard. It asks which e-reader you have, walks
through only the credentials that device needs, and validates each one
as you enter it: SMTP login test, Zotero key check with automatic
library ID lookup, full Dropbox OAuth exchange.

```bash
uv sync && uv run paperboy setup
```

How much setup you need depends on the device:

| You have | Credentials needed |
|---|---|
| Kindle | 2 — Send-to-Kindle address + an SMTP app password |
| PocketBook | 2 — Send-to-PocketBook address + an SMTP app password |
| Kobo | a Dropbox app (key/secret + one OAuth approval) + a contact email |
| + Zotero queue (optional) | 1 — a Zotero API key (library ID auto-detected) |
| + claude.ai / mobile (optional) | auto-generated token + a cloud deploy |

If you'd rather set up by hand, `cp .env.example .env` and fill it in;
every variable is documented there. Then register with Claude Code:

```bash
claude mcp add paperboy -- uv run --directory /path/to/paperboy paperboy
```

`--directory` matters: the server loads `.env` from its working
directory (set `PAPERBOY_ENV=/path/to/.env` to point elsewhere).

If paperboy is added but half-configured, ask Claude to "check my
paperboy setup". The `setup_status` tool reports what's missing and
what to do next, without passing secrets through the chat.

## Development

Managed with [uv](https://docs.astral.sh/uv/); linted and formatted with
ruff (Google style), type-checked with [ty](https://docs.astral.sh/ty/),
tested with pytest behind an enforced 80% coverage gate.

```bash
uv sync                    # install runtime + dev dependencies
uv run pre-commit install  # ruff, ty, uv-lock, hygiene hooks on commit
uv run pytest              # tests with coverage
uv run ruff check src tests && uv run ty check
```

## Remote deployment (Cloud Run)

### You were given a URL and a token

If someone shared their deployment with you, this is your whole setup:

```bash
claude mcp add --transport http paperboy <URL>/mcp \
  --header "Authorization: Bearer <token>"
```

The URL needs the `/mcp` path suffix. Treat the token like a password:
it lets you act fully as the owner — send email from their address,
deliver to their e-reader, and read and edit their Zotero library.
There is no reduced-permission mode; if that's not what you both want,
deploy your own instance (below). First calls after idle are quick;
the service scales to zero but starts in about a second.

### Deploy your own

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

Security model: when `PORT` is set (Cloud Run sets it), paperboy serves
Streamable HTTP at `/mcp` and requires `MCP_AUTH_TOKEN`. It refuses to
start unauthenticated because the server can send email as you.
Requests without the token get a 401 before any tool logic runs.
Ingress is public because Claude clients can't send Google IAM tokens;
the bearer token is the gate. Note that the budget emails you at its
thresholds but does not cap billing, so act on the 50% alert if it
ever fires.

### Connecting clients to the remote server

Clients differ in whether they accept the static bearer token, so
check your target client before deploying:

| Client | Works today? |
|---|---|
| Claude Code | Yes: `claude mcp add --transport http paperboy <URL>/mcp --header "Authorization: Bearer <token>"` |
| Claude API (MCP connector) | Yes: `authorization_token` parameter |
| claude.ai & mobile (any plan) | Check your Add-connector dialog. Anthropic is rolling out a beta "Request headers" section that accepts bearer tokens ([docs](https://claude.com/docs/connectors/custom/remote-mcp)); if your dialog shows it, paste `Authorization: Bearer <token>` there. If it shows only URL + OAuth client fields, you need the OAuth path: FastMCP ships provider integrations (Google, GitHub, Auth0, ...) that slot into `mcp.auth`. On the roadmap |

Client support changes often. Your own connector dialog is the
authority, not this table.

### How credentials flow

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

## Roadmap

- [ ] reMarkable delivery backend (real cloud API)
- [ ] arXiv HTML → EPUB via pandoc for reflowable reading (opt-in per
      paper; conversion is lossy for dense math, so PDF stays the
      default)
- [ ] Kindle highlights → Zotero notes round-trip (`My Clippings.txt`
      parser with fuzzy title matching)
- [ ] OAuth (instead of static bearer token) via FastMCP auth providers

## Prior art & acknowledgments

paperboy contains no code from other projects; everything in `src/` is
original. The ideas and services it builds on:

**Inspiration** (no code reused):

- [stakats/zotero-to-kindle](https://github.com/stakats/zotero-to-kindle)
  (no license file) — the tag-driven Zotero→Kindle idea, circa 2011,
  by one of Zotero's original directors
- [wahiggins3/send-to-kindle-mcp](https://github.com/wahiggins3/send-to-kindle-mcp)
  (MIT) — markdown→EPUB→Kindle, no library awareness
- [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)
  (MIT) — multi-source paper search/download
- [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp) (MIT) —
  mature Zotero library management, and the model for our `setup`
  wizard. paperboy leaves library management to it

**Dependencies** (all permissive, MIT-compatible):
[FastMCP](https://github.com/jlowin/fastmcp) (Apache-2.0),
[pyzotero](https://github.com/urschrei/pyzotero) (Blue Oak 1.0.0),
[httpx](https://github.com/encode/httpx) (BSD-3-Clause).

**Data & APIs**: Thank you to [arXiv](https://arxiv.org) for use of its
open access interoperability. Paper metadata and open-access links come
from [OpenAlex](https://openalex.org) (CC0),
[Crossref](https://www.crossref.org) (open metadata), and
[Unpaywall](https://unpaywall.org) (CC0 data), all run as open
scholarly infrastructure. Recommendations via the
[Semantic Scholar](https://www.semanticscholar.org) Recommendations
API (Allen Institute for AI). Library management via the
[Zotero](https://www.zotero.org) web API.
