# paperboy

[![CI](https://github.com/michaelellis003/paperboy/actions/workflows/ci.yml/badge.svg)](https://github.com/michaelellis003/paperboy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![paperboy MCP server](https://glama.ai/mcp/servers/michaelellis003/paperboy/badges/score.svg)](https://glama.ai/mcp/servers/michaelellis003/paperboy)

![Claude finding three papers, filing them in Zotero, and sending two to a Kindle](assets/paperboy-demo.gif)

An MCP server that delivers research papers to your e-reader, with Zotero
as the source of truth. Ask Claude for a reading list, then say "queue
them and send to my Kindle." (MCP is the plugin protocol Claude uses:
this program runs on your machine or your cloud project, and Claude
calls its tools during conversation.) Works locally in Claude Code and
Claude Desktop; a Cloud Run deployment adds claude.ai and the Claude
mobile app, so papers can be sent from a phone
([docs/deploy.md](docs/deploy.md)).

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

Papers are resolved by arXiv id, DOI, or bare title (high-confidence
fuzzy match only, so a reading list Claude produced in conversation can
be sent directly). Search runs against OpenAlex (~250M works) or arXiv.
When a paper can't be resolved or has no open-access PDF, the tool says
so in its receipt and Claude relays that to you. Nothing is dropped
silently.

### Delivery backends

| Backend | Devices | How |
|---|---|---|
| `email` (default) | Kindle, PocketBook, anything with an email intake | SMTP to the device address. Kindle constraints enforced: 25 attachments / 50 MB per email; sender must be on the Approved Personal Document E-mail List |
| `dropbox` | Kobo (native Dropbox sync on the device) | Uploads via the Dropbox API. Kobo only syncs `Apps/Rakuten Kobo/`, so use a **Full Dropbox**-scoped app with `DROPBOX_FOLDER="/Apps/Rakuten Kobo"`; an App-folder-scoped app can't reach it |

## Tools

| Tool | What it does |
|---|---|
| `search_papers` | Search OpenAlex (general) or arXiv (`source="arxiv"`); results carry a `ref` and an `open_access_pdf` flag |
| `recommend_papers` | Discover related or new papers: citation-graph recommendations (Semantic Scholar) seeded from your Zotero library, plus keyword discovery from interests Claude distills out of the conversation. Excludes papers you already have |
| `send_papers` | One-off send by arXiv id, DOI, URL, or title (also records in Zotero if configured) |
| `queue_papers` | Add papers to the Zotero Reading Queue without sending (optionally filed into topical collections) |
| `list_collections` | List Zotero collections so Claude can propose where to file a paper, or ask you |
| `file_papers` | File queued papers into a topical collection (created on demand; queue membership unaffected) |
| `unfile_papers` | Remove papers from one collection — for misfiled items; the papers themselves and their queue/sent state are untouched |
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

## Remote use

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
deploy your own instance.

### Deploy your own

One script creates a locked-down, single-tenant Cloud Run service that
normally costs $0 to run:

```bash
uv run paperboy setup && ./deploy/deploy.sh my-paperboy-project
```

Claude Code and API clients connect with the bearer token; claude.ai
and the mobile app connect through a Google sign-in restricted to your
account (one extra one-time setup step — the script prints it).
[docs/deploy.md](docs/deploy.md) covers what the script sets up, the
security model, and cost bounds.

## Development

uv for packaging, ruff (Google style), ty, pytest behind an enforced
80% coverage gate. `uv sync && uv run pre-commit install`, then
`uv run pytest`.

## Roadmap

- [ ] reMarkable delivery backend (real cloud API)
- [ ] arXiv HTML → EPUB via pandoc for reflowable reading (opt-in per
      paper; conversion is lossy for dense math, so PDF stays the
      default)
- [ ] Kindle highlights → Zotero notes round-trip (`My Clippings.txt`
      parser with fuzzy title matching)
- [x] Google OAuth for claude.ai and mobile connectors (see
      [docs/deploy.md](docs/deploy.md))

## Prior art & acknowledgments

Ideas paperboy builds on: the tag-driven Zotero→Kindle idea from
[stakats/zotero-to-kindle](https://github.com/stakats/zotero-to-kindle)
(circa 2011, by one of Zotero's original directors);
[wahiggins3/send-to-kindle-mcp](https://github.com/wahiggins3/send-to-kindle-mcp);
[openags/paper-search-mcp](https://github.com/openags/paper-search-mcp);
and [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp), the
model for our setup wizard — paperboy leaves library management to it.

Thank you to [arXiv](https://arxiv.org) for use of its open access
interoperability. Paper metadata and open-access links come from
[OpenAlex](https://openalex.org), [Crossref](https://www.crossref.org),
and [Unpaywall](https://unpaywall.org), all run as open scholarly
infrastructure. Recommendations via the
[Semantic Scholar](https://www.semanticscholar.org) Recommendations API
(Allen Institute for AI). Library management via the
[Zotero](https://www.zotero.org) web API. Built on
[FastMCP](https://github.com/jlowin/fastmcp),
[pyzotero](https://github.com/urschrei/pyzotero), and
[httpx](https://github.com/encode/httpx).

paperboy was built with [Claude Code](https://claude.com/claude-code).
