"""paperboy — MCP server that delivers research papers to your e-reader.

Runs over stdio for local use (Claude Code / Desktop) or Streamable HTTP
for remote use (claude.ai and mobile via a custom connector). Cloud Run
sets PORT, which switches on the HTTP transport automatically.
"""

import os
import sys

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from . import arxiv, delivery, openalex, resolver, zotero_client
from .config import settings
from .models import Paper

mcp = FastMCP(
    "paperboy",
    instructions=(
        "Delivers research papers to the user's e-reader (Kindle, Kobo, "
        "PocketBook, ...) and manages their Zotero reading queue. Prefer "
        "queue_papers + send_queue so state lives in Zotero; use "
        "send_papers for one-off sends. Papers are referenced by arXiv "
        "id, DOI, URL, or title — so a reading list from research can "
        "be sent directly. Always relay delivery receipts, including "
        "papers that could not be resolved or sent, back to the user."
    ),
)


def _summary(paper: Paper) -> dict:
    return {
        "ref": paper.doi or paper.arxiv_id or paper.url,
        "title": paper.title,
        "authors": paper.authors,
        "published": paper.published,
        "abstract": paper.abstract[:500],
        "open_access_pdf": bool(paper.pdf_url),
    }


@mcp.tool
def search_papers(
    query: str, max_results: int = 5, source: str = "all"
) -> list[dict]:
    """Search for papers across the scholarly literature.

    ``source`` is 'all' (OpenAlex: journals, conferences, and preprint
    servers including arXiv) or 'arxiv' (arXiv's own search, better for
    very recent preprints). Each result includes a ``ref`` (DOI or
    arXiv id) that can be passed to send_papers / queue_papers, and
    ``open_access_pdf`` indicating whether it can be delivered.
    """
    if source == "arxiv":
        papers = arxiv.search(query, max_results=max_results)
    else:
        papers = openalex.search(query, max_results=max_results)
    return [_summary(paper) for paper in papers]


def _resolve_all(refs: list[str]) -> tuple[list[Paper], list[str]]:
    """Resolve each ref independently; failures never block the rest."""
    resolved, unresolved = [], []
    for ref in refs:
        try:
            resolved.append(resolver.resolve(ref))
        except ValueError:
            unresolved.append(ref)
    return resolved, unresolved


@mcp.tool
def send_papers(papers: list[str]) -> str:
    """Send papers straight to the e-reader.

    Accepts arXiv ids ('2401.12345', 'arXiv:...'), abs/pdf URLs, DOIs
    like '10.1038/...', doi.org URLs, and paper titles (matched via
    OpenAlex; near-exact titles work best). Unresolvable papers and
    papers without an open-access PDF are reported back instead of
    sent — relay those to the user. If Zotero is configured, each sent
    paper is also added to the Reading Queue and tagged as sent, so
    the library stays the source of truth.
    """
    resolved, unresolved = _resolve_all(papers)
    sendable = [paper for paper in resolved if paper.pdf_url]
    no_pdf = [paper for paper in resolved if not paper.pdf_url]

    if not sendable:
        problems = [f"no open-access PDF: {p.title}" for p in no_pdf] + [
            f"could not resolve: {ref}" for ref in unresolved
        ]
        return "Nothing was sent. " + "; ".join(problems)

    documents = [
        (paper.safe_filename, resolver.download_pdf(paper))
        for paper in sendable
    ]
    receipt = delivery.send_documents(documents)

    if settings().zotero_enabled:
        for paper in resolved:
            item_key = zotero_client.add_paper(paper)
            if paper.pdf_url:
                zotero_client.mark_sent(item_key)
        receipt += " (recorded in Zotero Reading Queue)"
    if no_pdf:
        titles = "; ".join(paper.title for paper in no_pdf)
        receipt += f" | No open-access PDF, not sent: {titles}"
    if unresolved:
        receipt += f" | Could not resolve: {'; '.join(unresolved)}"
    return receipt


@mcp.tool
def queue_papers(papers: list[str]) -> str:
    """Add papers to the Zotero Reading Queue without sending them.

    Accepts arXiv ids, DOIs, URLs, or paper titles. Deduplicates
    against the existing queue. Unresolvable papers are reported back
    — relay those to the user.
    """
    resolved, unresolved = _resolve_all(papers)
    for paper in resolved:
        zotero_client.add_paper(paper)
    if not resolved:
        return "Nothing was queued. Could not resolve: " + "; ".join(unresolved)
    titles = "; ".join(paper.title for paper in resolved)
    queue = settings().reading_queue_collection
    receipt = f"Queued {len(resolved)} paper(s) in '{queue}': {titles}"
    if unresolved:
        receipt += f" | Could not resolve: {'; '.join(unresolved)}"
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
        "zotero_configured": cfg.zotero_enabled,
        "next_steps": next_steps,
    }


@mcp.tool
def send_queue() -> str:
    """Send every unsent paper in the Zotero Reading Queue to the e-reader.

    Items already tagged as sent are skipped. Items resolve via their
    DOI or URL; items with neither, or with no open-access PDF, are
    reported back for manual handling.
    """
    items = zotero_client.unsent_queue_items()
    if not items:
        return "Reading Queue is empty (or everything was already sent)."

    documents, sendable, skipped = [], [], []
    for item in items:
        data = item["data"]
        title = data.get("title", item["key"])
        ref = data.get("DOI") or data.get("url") or ""
        if not ref:
            skipped.append(f"{title} (no DOI or URL)")
            continue
        try:
            paper = resolver.resolve(ref)
        except ValueError:
            skipped.append(f"{title} (unresolvable: {ref})")
            continue
        if not paper.pdf_url:
            skipped.append(f"{title} (no open-access PDF)")
            continue
        documents.append((paper.safe_filename, resolver.download_pdf(paper)))
        sendable.append(item["key"])

    if not documents:
        skipped_titles = "; ".join(skipped)
        return f"Nothing in the queue is deliverable. Skipped: {skipped_titles}"

    receipt = delivery.send_documents(documents)
    for item_key in sendable:
        zotero_client.mark_sent(item_key)
    if skipped:
        receipt += f" | Skipped: {'; '.join(skipped)}"
    return receipt


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
    port = os.environ.get("PORT")
    if port:
        mcp.auth = _bearer_auth()
        mcp.run(transport="http", host="0.0.0.0", port=int(port))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
