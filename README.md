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
| `send_queue` | Send every unsent queue item, then tag as sent |

## Development

Managed with [uv](https://docs.astral.sh/uv/); linted/formatted with ruff
(Google style), type-checked with [ty](https://docs.astral.sh/ty/), tested
with pytest (coverage gate: 80%, currently ~98%).

```bash
uv sync                    # install runtime + dev dependencies
uv run pre-commit install  # ruff, ty, uv-lock, hygiene hooks on commit
uv run pytest              # tests with coverage
uv run ruff check src tests && uv run ty check
```

## Local setup

```bash
cp .env.example .env   # fill in SMTP creds + device address
set -a; source .env; set +a
uv run paperboy        # stdio transport
```

Register with Claude Code:

```bash
claude mcp add paperboy -- uv run --directory /path/to/paperboy paperboy
```

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

## Prior art

- [stakats/zotero-to-kindle](https://github.com/stakats/zotero-to-kindle) —
  the same tag-driven idea, circa 2011, pre-MCP
- [wahiggins3/send-to-kindle-mcp](https://github.com/wahiggins3/send-to-kindle-mcp) —
  markdown→EPUB→Kindle, no library awareness
- [openags/paper-search-mcp](https://github.com/openags/paper-search-mcp) —
  multi-source paper search/download
- The Zotero MCP ecosystem (e.g. 54yyyu/zotero-mcp) — mature library
  management; paperboy deliberately does *not* compete with it
