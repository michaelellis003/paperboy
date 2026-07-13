## What & why

<!-- What does this change, and what problem does it solve? Link the
issue if there is one. -->

## How it was verified

<!-- `uv run pytest`, `uv run ruff check`, `uv run ty check` all pass
locally. For behavior changes: which test fails without this change? -->

## Checklist

- [ ] Tests cover the change (bug fixes: a test that fails without it)
- [ ] Receipts still tell the truth (no claim of a mutation that didn't
      happen, none omitted, no contradictions)
- [ ] Zotero interactions go through `FakeZotero` in tests, and the fake
      wasn't loosened
- [ ] No new dependencies (or an issue discussed them first)
