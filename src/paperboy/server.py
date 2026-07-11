"""paperboy — MCP server that delivers research papers to your e-reader.

Runs over stdio for local use (Claude Code / Desktop) or Streamable HTTP
for remote use (claude.ai and mobile via a custom connector). Cloud Run
sets PORT, which switches on the HTTP transport automatically.
"""

import os
import sys

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from . import arxiv, delivery, openalex, resolver, zotero_client
from .config import settings
from .models import Paper, normalize_title

mcp = FastMCP(
    "paperboy",
    instructions=(
        "Delivers research papers to the user's e-reader (Kindle, Kobo, "
        "PocketBook, ...) and organizes them in a Zotero reading queue. "
        "Papers are referenced by arXiv id, DOI, arXiv/doi.org URL, or "
        "title (publisher landing URLs won't resolve) — so a reading "
        "list from research can be sent directly. Use "
        "send_papers when the user picks specific papers; send_queue "
        "flushes EVERY unsent queued item, so check list_queue first. "
        "Always relay delivery receipts — including sizes, skipped "
        "papers, and failures — back to the user."
    ),
)


def _summary(paper: Paper) -> dict:
    authors = paper.authors[:3]
    if len(paper.authors) > 3:
        authors = [*authors, "et al."]
    return {
        # arXiv id first (most reliable delivery), then DOI, then the
        # exact title, which the resolver accepts — a bare landing URL
        # would not round-trip into send_papers.
        "ref": paper.arxiv_id or paper.doi or paper.title,
        "title": paper.title,
        "authors": authors,
        "published": paper.published,
        "abstract": paper.abstract[:300],
        "open_access_pdf": bool(paper.pdf_url),
    }


@mcp.tool
def search_papers(
    query: str, max_results: int = 5, source: str = "all"
) -> list[dict]:
    """Search for papers across the scholarly literature.

    ``source`` is 'all' (OpenAlex: journals, conferences, and preprint
    servers including arXiv — usually the better ranking, even for
    arXiv-native topics) or 'arxiv' (arXiv's own search; only better
    for very recent preprints); unknown values fall back to 'all'.
    ``max_results`` is capped at 25. Each result has a ``ref`` (arXiv
    id, DOI, or exact title) to pass to send_papers / queue_papers.
    ``open_access_pdf`` means an OA PDF link was found; delivery can
    still fail if the link is dead (arXiv-hosted papers are the most
    reliable).
    """
    max_results = min(max_results, 25)
    try:
        if source == "arxiv":
            papers = arxiv.search(query, max_results=max_results)
        else:
            papers = openalex.search(query, max_results=max_results)
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Search failed ({type(exc).__name__}): {exc} — retry, or "
            "rephrase the query"
        ) from exc
    return [_summary(paper) for paper in papers]


def _oa_hint() -> str:
    """Warn when open-access lookup is disabled by missing config."""
    if settings().polite_email:
        return ""
    return (
        " [open-access PDF lookup is disabled: no contact email is "
        "configured — set CONTACT_EMAIL or run 'paperboy setup']"
    )


def _resolve_all(refs: list[str]) -> tuple[list[Paper], list[str]]:
    """Resolve refs independently, deduplicating within the call.

    Returns (papers, problems) where problems are human-readable
    strings distinguishing bad refs from transient network failures.
    """
    resolved: list[Paper] = []
    seen: set[str] = set()
    problems: list[str] = []
    for ref in refs:
        try:
            paper = resolver.resolve(ref)
        except ValueError as exc:
            # The resolver's message is self-contained and may carry an
            # actionable hint (e.g. "publisher landing URLs are not
            # supported") — pass it through verbatim.
            problems.append(str(exc))
            continue
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                # 4xx is deterministic — retrying will never succeed.
                problems.append(
                    f"could not resolve: {ref} (backend rejected the "
                    f"request: HTTP {exc.response.status_code})"
                )
            else:
                problems.append(
                    f"temporarily unreachable ({type(exc).__name__}): "
                    f"{ref} — retry"
                )
            continue
        except httpx.HTTPError as exc:
            problems.append(
                f"temporarily unreachable ({type(exc).__name__}): {ref} — retry"
            )
            continue
        # A paper referenced by DOI and by arXiv id may share no id
        # fields, so every identity — including the normalized title —
        # participates in dedup.
        keys = {
            key
            for key in (
                paper.doi,
                paper.arxiv_id,
                normalize_title(paper.title),
            )
            if key
        }
        if keys & seen:
            continue
        seen |= keys
        resolved.append(paper)
    return resolved, problems


def _download_all(
    papers: list[Paper],
) -> tuple[list[tuple[Paper, bytes]], list[str]]:
    """Download PDFs one by one; a dead link never blocks the batch."""
    downloaded, failures = [], []
    for paper in papers:
        try:
            downloaded.append((paper, resolver.download_pdf(paper)))
        except (ValueError, httpx.HTTPError) as exc:
            failures.append(f"download failed: {paper.title} ({exc})")
    return downloaded, failures


def _chunk(
    documents: list[tuple[str, bytes]],
) -> list[list[tuple[str, bytes]]]:
    """Split documents into batches within email limits."""
    batches: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    size = 0
    for name, content in documents:
        if current and (
            len(current) >= delivery.MAX_ATTACHMENTS
            or size + len(content) > delivery.CHUNK_TARGET_BYTES
        ):
            batches.append(current)
            current, size = [], 0
        current.append((name, content))
        size += len(content)
    if current:
        batches.append(current)
    return batches


def _deliver(documents: list[tuple[str, bytes]]) -> tuple[str, set[str]]:
    """Send documents, auto-splitting into limit-sized batches.

    Returns (receipt, delivered filenames) — a failed batch never
    hides the batches that did go out, so callers can mark exactly
    what was sent and never re-ship it.
    """
    receipts: list[str] = []
    delivered: set[str] = set()
    for batch in _chunk(documents):
        try:
            receipts.append(delivery.send_documents(batch))
            delivered.update(name for name, _ in batch)
        except delivery.DeliveryError as exc:
            receipts.append(f"delivery failed: {exc}")
    if len(receipts) > 1:
        return " || ".join(receipts), delivered
    return receipts[0], delivered


@mcp.tool
def send_papers(
    papers: list[str], force: bool = False, dry_run: bool = False
) -> str:
    """Send papers to the e-reader by arXiv id, DOI, or title.

    Accepted refs: arXiv ids ('2401.12345', 'arXiv:...'), arXiv
    abs/pdf URLs, DOIs, doi.org URLs, and paper titles. Publisher
    landing-page URLs are NOT resolvable — use the DOI or title.

    Refs are deduplicated, and papers already tagged as sent in Zotero
    are skipped unless force=True. Large batches are split
    automatically to fit the 25-attachment / 50 MB per-email limits.
    dry_run=True previews what would be sent, with estimated sizes,
    without downloading or delivering — use it before big sends.

    Papers without an open-access PDF are not sent; if Zotero is
    configured they are still queued (tagged no-oa-pdf) so they can be
    delivered manually later. Relay the full receipt — sizes, skips,
    and failures — to the user.
    """
    resolved, problems = _resolve_all(papers)

    already_sent: list[Paper] = []
    if settings().zotero_enabled and not force:
        remaining = []
        for paper in resolved:
            item = zotero_client.find_item(paper)
            if item is not None and zotero_client.is_sent(item):
                already_sent.append(paper)
            else:
                remaining.append(paper)
        resolved = remaining

    sendable = [paper for paper in resolved if paper.pdf_url]
    no_pdf = [paper for paper in resolved if not paper.pdf_url]

    if dry_run:
        lines = []
        total = 0.0
        unknown = 0
        for paper in sendable:
            size = resolver.probe_pdf_size(paper)
            mb = f"{size / 1e6:.1f} MB" if size else "size unknown"
            total += (size or 0) / 1e6
            unknown += 0 if size else 1
            lines.append(f"{paper.title} ({mb})")
        headline = f"Would send {len(sendable)} paper(s), ~{total:.1f} MB"
        if unknown:
            headline += f" + {unknown} of unknown size"
        parts = [
            headline + ": " + "; ".join(lines)
            if lines
            else "Nothing would be sent."
        ]
        if no_pdf:
            parts.append(
                "no open-access PDF: " + "; ".join(p.title for p in no_pdf)
            )
        if already_sent:
            parts.append(
                "already sent (use force=True to resend): "
                + "; ".join(p.title for p in already_sent)
            )
        parts.extend(problems)
        return " | ".join(parts)

    downloaded, failures = _download_all(sendable)
    problems.extend(failures)

    if not downloaded:
        queued_note = ""
        if settings().zotero_enabled and resolved:
            for paper in resolved:
                item_key, _ = zotero_client.add_paper(paper)
                if not paper.pdf_url:
                    zotero_client.mark_no_pdf(item_key)
            queued_note = " (queued unsent in Zotero Reading Queue)"
        skips = [f"no open-access PDF: {p.title}" for p in no_pdf] + [
            f"already sent (use force=True to resend): {p.title}"
            for p in already_sent
        ]
        return (
            "Nothing was sent."
            + queued_note
            + " "
            + "; ".join(skips + problems)
            + (_oa_hint() if no_pdf else "")
        )

    documents = [
        (paper.safe_filename, content) for paper, content in downloaded
    ]
    receipt, delivered = _deliver(documents)

    if settings().zotero_enabled:
        for paper in resolved:
            item_key, _ = zotero_client.add_paper(paper)
            if paper.safe_filename in delivered:
                zotero_client.mark_sent(item_key)
            elif not paper.pdf_url:
                zotero_client.mark_no_pdf(item_key)
        receipt += " (recorded in Zotero Reading Queue)"
    if no_pdf:
        titles = "; ".join(paper.title for paper in no_pdf)
        note = (
            "No open-access PDF, queued unsent"
            if settings().zotero_enabled
            else "No open-access PDF, not sent"
        )
        receipt += f" | {note}: {titles}{_oa_hint()}"
    if already_sent:
        titles = "; ".join(paper.title for paper in already_sent)
        receipt += f" | Already sent, skipped (force=True to resend): {titles}"
    if problems:
        receipt += f" | Problems: {'; '.join(problems)}"
    return receipt


@mcp.tool
def queue_papers(papers: list[str]) -> str:
    """Add papers to the Zotero Reading Queue without sending them.

    Accepts arXiv ids, DOIs, URLs, or paper titles. Papers already in
    the queue are reported as such, not re-added. Unresolvable papers
    are reported back — relay those to the user.
    """
    zotero_client.ensure_configured()
    resolved, problems = _resolve_all(papers)
    new, existing, no_pdf = [], [], []
    for paper in resolved:
        item_key, created = zotero_client.add_paper(paper)
        (new if created else existing).append(paper.title)
        if not paper.pdf_url:
            zotero_client.mark_no_pdf(item_key)
            no_pdf.append(paper.title)
    if not resolved:
        return "Nothing was queued. " + "; ".join(problems)
    queue = settings().reading_queue_collection
    parts = [f"Queued {len(new)} new paper(s) in '{queue}'"]
    if new:
        parts[0] += ": " + "; ".join(new)
    if existing:
        parts.append(f"already in queue: {'; '.join(existing)}")
    if no_pdf:
        parts.append(
            "no open-access PDF (won't be auto-sent): "
            + "; ".join(no_pdf)
            + _oa_hint()
        )
    if problems:
        parts.append("; ".join(problems))
    return " | ".join(parts)


@mcp.tool
def list_queue() -> list[dict]:
    """List the Zotero Reading Queue with delivery status per item.

    Status is 'unsent', 'sent', or 'no-open-access-pdf'. Use this to
    show the user their queue, before send_queue (which flushes every
    unsent item), or to find refs for remove_from_queue.
    """
    return zotero_client.list_queue()


@mcp.tool
def remove_from_queue(refs: list[str]) -> str:
    """Remove papers from the Zotero Reading Queue by ref or title.

    Matches each ref (arXiv id, DOI, URL, or exact title) against
    queue items and deletes matches from the library. This does not
    delete anything from the e-reader.
    """
    removed, misses = zotero_client.remove_by_refs(refs)
    receipt = f"Removed {len(removed)} item(s) from the queue"
    if removed:
        receipt += ": " + "; ".join(removed)
    if misses:
        receipt += f" | Not found in queue: {'; '.join(misses)}"
    return receipt


@mcp.tool
def send_queue() -> str:
    """Send EVERY unsent paper in the Zotero Reading Queue.

    This flushes the whole queue — for specific papers use
    send_papers. Items tagged sent or no-oa-pdf are skipped; items
    whose PDF turns out to be unavailable are tagged no-oa-pdf so they
    are not retried forever. Batches are split under the email limits
    automatically. Check list_queue first when unsure what will go.
    """
    items = zotero_client.unsent_queue_items()
    if not items:
        return (
            "Reading Queue is empty (or everything was already sent; "
            "items tagged no-oa-pdf are excluded — see list_queue)."
        )

    downloaded, skipped = [], []
    for item in items:
        data = item["data"]
        title = data.get("title", item["key"])
        # Try the strongest identifier first, but fall back to the
        # stored title — items captured from landing-page-only works
        # have no DOI and a URL the resolver rejects.
        refs = [ref for ref in (data.get("DOI"), data.get("url"), title) if ref]
        if not refs:
            skipped.append(f"{title} (no DOI, URL, or title)")
            continue
        paper = None
        for ref in refs:
            try:
                paper = resolver.resolve(ref)
                break
            except (ValueError, httpx.HTTPError):
                continue
        if paper is None:
            skipped.append(f"{title} (unresolvable: {refs[0]})")
            continue
        if not paper.pdf_url:
            zotero_client.mark_no_pdf(item["key"])
            skipped.append(
                f"{title} (no open-access PDF — won't retry){_oa_hint()}"
            )
            continue
        try:
            content = resolver.download_pdf(paper)
        except (ValueError, httpx.HTTPError):
            zotero_client.mark_no_pdf(item["key"])
            skipped.append(f"{title} (PDF download failed — won't retry)")
            continue
        downloaded.append((item["key"], paper.safe_filename, content))

    if not downloaded:
        return "Nothing in the queue is deliverable. Skipped: " + "; ".join(
            skipped
        )

    documents = [(name, content) for _, name, content in downloaded]
    receipt, delivered = _deliver(documents)
    marked = [item_key for item_key, name, _ in downloaded if name in delivered]
    for item_key in marked:
        zotero_client.mark_sent(item_key)
    if marked:
        receipt += " (tagged sent in Zotero)"
    if skipped:
        receipt += f" | Skipped: {'; '.join(skipped)}"
    return receipt


@mcp.tool
def setup_status() -> dict:
    """Report which paperboy features are configured and what's missing.

    Returns configuration state only — never secret values. Use this to
    guide the user through finishing setup. Credentials themselves must
    be entered by running 'paperboy setup' in a terminal, never pasted
    into the chat.
    """
    cfg = settings()
    email_ready = all(
        [
            cfg.device_email,
            cfg.smtp_host,
            cfg.smtp_user,
            cfg.smtp_password,
            cfg.from_email,
        ]
    )
    dropbox_ready = all(
        [
            cfg.dropbox_app_key,
            cfg.dropbox_app_secret,
            cfg.dropbox_refresh_token,
        ]
    )
    delivery_ready = (
        email_ready if cfg.delivery_method == "email" else dropbox_ready
    )
    next_steps = []
    if not delivery_ready:
        next_steps.append(
            "Delivery is not configured — run 'paperboy setup' in a "
            "terminal; it asks which e-reader you have and walks "
            "through only the credentials that device needs."
        )
    if not cfg.polite_email:
        next_steps.append(
            "Set CONTACT_EMAIL (any email you own) — Unpaywall's "
            "open-access PDF lookup requires one; without it, "
            "non-arXiv papers cannot be delivered."
        )
    if not cfg.zotero_enabled:
        next_steps.append(
            "Optional: connect Zotero for the reading queue — "
            "'paperboy setup' covers it, or see README."
        )
    return {
        "delivery_method": cfg.delivery_method,
        "delivery_ready": delivery_ready,
        "email_backend_configured": email_ready,
        "dropbox_backend_configured": dropbox_ready,
        "open_access_lookup_ready": bool(cfg.polite_email),
        "zotero_configured": cfg.zotero_enabled,
        "next_steps": next_steps,
    }


def _bearer_auth() -> StaticTokenVerifier:
    """Build the bearer-token verifier required for the HTTP transport.

    The server can send email as the user, so remote access is never
    served unauthenticated. The token comes from MCP_AUTH_TOKEN (Cloud
    Run: mount from Secret Manager) and is pasted into the MCP client's
    Authorization header.
    """
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit(
            "HTTP transport requires MCP_AUTH_TOKEN (at least 32 chars). "
            "Generate one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(32))'"
        )
    return StaticTokenVerifier(
        tokens={token: {"client_id": "paperboy-owner", "scopes": []}}
    )


def main() -> None:
    """Run the server, or the setup wizard for ``paperboy setup``.

    Serves over stdio by default, or Streamable HTTP when PORT is set
    (Cloud Run does this).
    """
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        from . import setup_wizard

        setup_wizard.run(sys.argv[2:])
        return
    from .config import load_env_file

    load_env_file()
    port = os.environ.get("PORT")
    if port:
        mcp.auth = _bearer_auth()
        mcp.run(transport="http", host="0.0.0.0", port=int(port))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
