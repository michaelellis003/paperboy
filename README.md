# paperboy

An MCP server that delivers research papers to your e-reader, with Zotero as
the source of truth. Ask Claude for a reading list, then say "queue them and
send to my Kindle" — from any Claude client, including claude.ai and mobile.

## Architecture

Hub-and-spoke, with Zotero as the library hub and this server as the delivery
spoke. State lives in Zotero, not here:

- Papers land in a **Reading Queue** collection (created on demand).
- Delivery is recorded by tagging the item `sent-to-ereader`.
- The server itself is stateless — safe to redeploy, no database.

Papers are resolved by arXiv id (arXiv Atom API), DOI (Crossref metadata
plus Unpaywall open-access PDF lookup), or bare title (OpenAlex search,
accepted only on a high-confidence fuzzy match — so a reading list Claude
produced in-conversation can be sent directly, no ids needed). Search runs
against OpenAlex (~250M works across journals, conferences, and preprint
servers) or arXiv directly. Unresolvable or paywalled papers are never
silently dropped: every tool returns a receipt naming them, which Claude
relays back to you.

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
| `send_papers` | One-off send by arXiv id, DOI, URL, or title (also records in Zotero if configured) |
| `queue_papers` | Add papers to the Zotero Reading Queue without sending |
| `list_queue` | Show the queue with per-item status (unsent / sent / no-open-access-pdf) |
| `remove_from_queue` | Delete queue items by exact ref or title |
| `send_queue` | Send every unsent queue item (auto-split under email limits), then tag as sent |
| `setup_status` | Report what's configured / what's missing (no secrets) so Claude can guide setup |

## Development

Managed with [uv](https://docs.astral.sh/uv/); linted/formatted with ruff
(Google style), type-checked with [ty](https://docs.astral.sh/ty/), tested
with pytest behind an enforced 80% coverage gate.

```bash
uv sync                    # install runtime + dev dependencies
uv run pre-commit install  # ruff, ty, uv-lock, hygiene hooks on commit
uv run pytest              # tests with coverage
uv run ruff check src tests && uv run ty check
```

## Setup

Run the interactive wizard — it asks which e-reader you have and walks
through only the credentials that device needs, validating each one as
you enter it (SMTP login test, Zotero key check with automatic library
ID lookup, full Dropbox OAuth exchange):

```bash
uv sync && uv run paperboy setup
```

How much setup depends entirely on the device:

| You have | Credentials needed |
|---|---|
| Kindle | 2 — Send-to-Kindle address + an SMTP app password |
| PocketBook | 2 — Send-to-PocketBook address + an SMTP app password |
| Kobo | a Dropbox app (key/secret + one OAuth approval) |
| + Zotero queue (optional) | 1 — a Zotero API key (library ID auto-detected) |
| + claude.ai / mobile (optional) | auto-generated token + a cloud deploy |

Prefer manual? `cp .env.example .env` and fill it in; every variable is
documented there. Then register with Claude Code:

```bash
claude mcp add paperboy -- uv run --directory /path/to/paperboy paperboy
```

`--directory` matters: the server loads `.env` from its working
directory (set `PAPERBOY_ENV=/path/to/.env` to point elsewhere).

If paperboy is added but half-configured, ask Claude "check my paperboy
setup" — the `setup_status` tool reports what's missing and what to do,
without ever passing secrets through the chat.

## Remote deployment (Cloud Run)

When `PORT` is set (Cloud Run does this), paperboy serves Streamable HTTP at
`/mcp` — which is what claude.ai custom connectors expect — and **requires**
`MCP_AUTH_TOKEN`: it refuses to start unauthenticated, because the server can
send email as you. Put every value from `.env.example` into Secret Manager /
Cloud Run env vars.

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'  # MCP_AUTH_TOKEN
gcloud run deploy paperboy --source . --region us-central1 --allow-unauthenticated
```

(`--allow-unauthenticated` here means Cloud Run itself doesn't require a
Google identity — the bearer token is what gates access. Keep it secret.)

Then add it in claude.ai → Settings → Connectors → Add custom connector with
the service URL + `/mcp`, supplying the token as the Authorization header
(`Bearer <token>`). For OAuth instead of a static token, FastMCP ships
provider integrations (Google, GitHub, Auth0, ...) that slot into `mcp.auth`.

## Roadmap

- [ ] reMarkable delivery backend (real cloud API)
- [ ] arXiv HTML → EPUB via pandoc for reflowable reading (opt-in per paper —
      conversion is lossy for dense math, so PDF stays the default)
- [ ] Kindle highlights → Zotero notes round-trip (`My Clippings.txt` parser
      with fuzzy title matching)
- [ ] OAuth (instead of static bearer token) via FastMCP auth providers

## Prior art & acknowledgments

paperboy contains no code from other projects — everything in `src/` is
original — but it stands on ideas and services worth crediting:

**Inspiration** (no code reused):

- [stakats/zotero-to-kindle](https://github.com/stakats/zotero-to-kindle)
  (no license file) — the tag-driven Zotero→Kindle idea, circa 2011,
  pre-MCP, by one of Zotero's original directors
- [wahiggins3/send-to-kindle-mcp](https://github.com/wahiggins3/send-to-kindle-mcp)
  (MIT) — markdown→EPUB→Kindle, no library awareness
- [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)
  (MIT) — multi-source paper search/download
- [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp) (MIT) —
  mature Zotero library management, and the model for our `setup`
  wizard; paperboy deliberately does *not* compete with it

**Dependencies** (all permissive, MIT-compatible):
[FastMCP](https://github.com/jlowin/fastmcp) (Apache-2.0),
[pyzotero](https://github.com/urschrei/pyzotero) (Blue Oak 1.0.0),
[httpx](https://github.com/encode/httpx) (BSD-3-Clause).

**Data & APIs**: Thank you to [arXiv](https://arxiv.org) for use of its
open access interoperability. Paper metadata and open-access links come
from [OpenAlex](https://openalex.org) (CC0),
[Crossref](https://www.crossref.org) (open metadata), and
[Unpaywall](https://unpaywall.org) (CC0 data), all run as open
scholarly infrastructure. Library management via the
[Zotero](https://www.zotero.org) web API.
