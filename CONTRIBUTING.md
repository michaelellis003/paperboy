# Contributing to paperboy

Thanks for your interest. Bug reports, fixes, and features are all
welcome. This page covers how to get a working dev setup, what the
checks expect, and what makes a change easy to merge.

## Dev setup

You need [uv](https://docs.astral.sh/uv/). Then:

```bash
git clone https://github.com/michaelellis003/paperboy
cd paperboy
uv sync
uv run pre-commit install
```

That's it. The test suite runs entirely offline: no Zotero account,
no SMTP server, and no API keys are needed to develop. Credentials
only matter if you want to run the server against your own library
(`uv run paperboy setup` walks you through that).

## Running the checks

The same checks run locally, in pre-commit, and in CI:

```bash
uv run pytest                        # tests, with an enforced 80% coverage gate
uv run ruff check src tests         # lint (Google style)
uv run ruff format --check src tests
uv run ty check                     # types
```

A PR that passes these locally will pass CI.

## How the code is laid out

One module per concern, all under `src/paperboy/`:

| Module | Concern |
|---|---|
| `server.py` | The MCP tools and their receipts |
| `resolver.py` | Turning a ref (arXiv id, DOI, title, URL) into a paper |
| `books.py` | Book resolution (ISBN, book DOI, title) |
| `arxiv.py`, `doi.py`, `openalex.py`, `s2.py` | One registry backend each |
| `zotero_client.py` | Everything that reads or writes Zotero |
| `delivery.py` | Email and Dropbox delivery backends |
| `config.py` | Settings from the environment |

## Testing conventions

Tests never touch the network. Registry backends are exercised through
`httpx.MockTransport`, and Zotero through the `FakeZotero` class in
`tests/test_zotero_client.py`.

`FakeZotero` is deliberately strict: it mirrors real Zotero API
semantics, including version-guarded writes (a stale write raises,
like the real API's 412) and non-mutating PATCH calls. If your change
interacts with Zotero, drive it through the fake; if the fake is
missing a behavior the real API has, make the fake match the real API
rather than loosening it. Two production bugs shipped because an
earlier, friendlier fake hid them.

Behavior changes need a test. Bug fixes need a test that fails without
the fix.

## What makes a change easy to merge

- **Receipts must tell the truth.** Every tool returns a receipt
  describing what happened. A receipt must never claim a mutation that
  didn't happen, omit one that did, or contradict itself. This is the
  project's core invariant and most review comments trace back to it.
- **A wrong paper on the e-reader is worse than a lookup failure.**
  Resolution errs conservative: fuzzy matches below the confidence
  threshold are offered for confirmation, never acted on.
- **Small PRs.** One concern per PR. A fix plus its test is ideal.
- **No new dependencies** without an issue discussing it first. The
  runtime dependency list is four packages and we like it that way.
- **Match the style around you.** Google-style docstrings, 80-column
  lines, comments that state constraints the code can't show (not
  narration of what the next line does). Ruff enforces most of this.

## Reporting bugs

Use the bug report template. The most useful thing you can include is
the exact receipt text the tool returned, plus the output of the
`setup_status` tool (it never contains secrets). Never paste your
`.env`, API keys, or bearer tokens into an issue; see
[SECURITY.md](SECURITY.md).

## Proposing features

Open an issue before writing significant code, so scope gets agreed
first. The [README roadmap](README.md#roadmap) lists what's already
planned. Tool-surface changes (new tools, changed parameters) get extra
scrutiny because deployed clients depend on them.

## Releases

Maintainers handle releases: squash-merge to `main`, tag `vX.Y.Z`, and
publish a GitHub release. Contributors don't need to touch versioning.
