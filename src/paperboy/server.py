"""paperboy — MCP server that delivers research papers to your e-reader.

Runs over stdio for local use (Claude Code / Desktop) or Streamable HTTP
for remote use (claude.ai and mobile via a custom connector). Cloud Run
sets PORT, which switches on the HTTP transport automatically.
"""

import os
import sys
from itertools import zip_longest

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

from . import arxiv, delivery, openalex, resolver, s2, zotero_client
from .config import settings
from .models import Paper, normalize_title

mcp = FastMCP(
    "paperboy",
    instructions=(
        "Delivers research papers to the user's e-reader (Kindle, Kobo, "
        "PocketBook, ...) and organizes them in Zotero. Papers are "
        "referenced by arXiv id, DOI, arXiv/doi.org URL, or title "
        "(publisher landing URLs won't resolve) — so a reading list "
        "from research can be sent directly. Use send_papers when the "
        "user picks specific papers; send_queue flushes EVERY unsent "
        "queued item, so check list_queue first. Organization: when "
        "queueing/sending new papers, check list_collections and pass "
        "collections=[...] to file them topically — propose a fit from "
        "the paper's topic, and ASK THE USER when the fit is ambiguous "
        "rather than guessing. Discovery: recommend_papers finds "
        "related/new work from the user's library plus interests you "
        "distill from the conversation — present picks, don't send "
        "unasked. Always relay delivery receipts — including sizes, "
        "skipped papers, and failures — to the user."
    ),
)


def _shorten(text: str, limit: int = 300) -> str:
    """Truncate at a word boundary with a visible ellipsis."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


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
        "abstract": _shorten(paper.abstract),
        "open_access_pdf": bool(paper.pdf_url),
    }


@mcp.tool
def search_papers(
    query: str, max_results: int = 5, source: str = "all"
) -> list[dict]:
    """Search for papers across the scholarly literature.

    ``source`` is 'all' (OpenAlex: journals, conferences, and preprint
    servers including arXiv — broad coverage, but ranking can miss on
    arXiv-native topics) or 'arxiv' (arXiv's own search — better for
    recent preprints or when 'all' returns off-topic results); unknown
    values fall back to 'all'.
    ``max_results`` is clamped to the 1-25 range. Each result has a ``ref``
    (arXiv id, DOI, or exact title) to pass to send_papers /
    queue_papers.
    ``open_access_pdf`` means an OA PDF link was found; delivery can
    still fail if the link is dead (arXiv-hosted papers are the most
    reliable).
    """
    if not query.strip():
        # OpenAlex answers an empty query with its default ranking —
        # all-time most-cited papers — which would read as results.
        raise ValueError("Search needs a non-empty query.")
    max_results = max(1, min(max_results, 25))
    try:
        if source == "arxiv":
            papers = arxiv.search(query, max_results=max_results)
        else:
            papers = openalex.search(query, max_results=max_results)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            raise RuntimeError(
                "The search backend is rate-limiting this connection. "
                "Wait a minute, or use source='arxiv' — rephrasing "
                "won't help."
            ) from exc
        raise RuntimeError(
            f"Search failed ({type(exc).__name__}): {exc} — retry, or "
            "rephrase the query"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Search failed ({type(exc).__name__}): {exc} — retry, or "
            "rephrase the query"
        ) from exc
    return [_summary(paper) for paper in papers if _is_usable(paper)]


def _identity_keys(paper: Paper) -> set[str]:
    return {
        key
        for key in (
            paper.doi and paper.doi.lower(),
            paper.arxiv_id,
            normalize_title(paper.title),
        )
        if key
    }


def _is_usable(paper: Paper) -> bool:
    """Reject malformed upstream records before they reach the user.

    Some sources emit truncated junk like 'UvA-DARE (' as both ref and
    title. A result is only useful if it carries a resolvable id, or a
    title long enough to round-trip through title resolution.
    """
    if paper.arxiv_id or paper.doi:
        return True
    title = paper.title.strip()
    return (
        len(title) >= 10
        and not title.startswith("(")  # "(untitled)"
        and not title.endswith("(")  # truncated, e.g. "UvA-DARE ("
    )


@mcp.tool
def recommend_papers(
    seed_refs: list[str] | None = None,
    interests: list[str] | None = None,
    recent_only: bool = True,
    max_results: int = 8,
) -> dict:
    """Discover papers the user may want to read — old or new.

    Blends two signals: citation-graph recommendations (Semantic
    Scholar) seeded from ``seed_refs``, or — by default — the user's
    Zotero library (what they queue and read IS their interest
    profile); and keyword discovery (OpenAlex) from ``interests`` —
    pass 2-4 short phrases distilled from the current conversation.
    recent_only=True favors newly published work; False searches the
    all-time pool (computer science only, an upstream limit).
    Papers already in the user's library are excluded (the queue plus
    the 100 most recently added items). max_results is capped at 20.

    Returns {"picks": [...], "problems": [...]}: picks carry refs for
    send_papers / queue_papers, and each pick has a ``via`` field
    saying why it appeared — 'interest-keyword' (matched a stated
    interest), 'related-to-seeds' (citation graph of explicit seeds),
    or 'related-to-library' (citation graph of the OWNER'S Zotero
    library; can look off-topic to anyone else). When interests are
    given they lead the results. problems reports any discovery arm
    that failed or seed that didn't resolve — ALWAYS relay problems,
    or a stated interest may silently go uncovered. Present picks and
    let the user choose; don't send unasked.
    """
    max_results = max(1, min(max_results, 20))
    problems: list[str] = []

    seeds: list[str] = []
    if seed_refs:
        resolved, problems = _resolve_all(seed_refs)
        for paper in resolved:
            if paper.arxiv_id:
                seeds.append(f"ArXiv:{paper.arxiv_id}")
            elif paper.doi:
                seeds.append(f"DOI:{paper.doi}")
    elif settings().zotero_enabled:
        seeds = zotero_client.seed_ids(limit=10)
        if not seeds and not interests:
            raise RuntimeError(
                "No discovery signal: the Reading Queue has no papers "
                "with arXiv ids or DOIs to seed from — pass seed_refs "
                "and/or interests."
            )
    if not seeds and not interests:
        prefix = "; ".join(problems) + " — " if problems else ""
        raise RuntimeError(
            f"No discovery signal: {prefix}pass resolvable seed_refs "
            "and/or interests, or configure Zotero so the library can "
            "seed recommendations."
        )

    graph_arm: list[Paper] = []
    keyword_arm: list[Paper] = []
    if seeds:
        pool = "recent" if recent_only else "all-cs"
        try:
            graph_arm = s2.recommend(seeds, pool=pool, limit=max_results * 2)
        except httpx.HTTPError as exc:
            problems.append(
                "citation-graph arm unreachable "
                f"({type(exc).__name__}) — its picks are missing"
            )
    # Round-robin across interest phrases so that, with small
    # max_results, later phrases aren't starved by the first.
    per_phrase = [
        _interest_results(phrase, problems) for phrase in (interests or [])[:4]
    ]
    for group in zip_longest(*per_phrase):
        keyword_arm.extend(paper for paper in group if paper is not None)

    # Interleave the arms so neither starves the other within
    # max_results. Each candidate keeps its arm so picks can say why
    # they appeared: graph picks from explicit seeds are
    # "related-to-seeds"; graph picks from the library say so, because
    # to anyone who isn't the library's owner they can look random.
    graph_via = "related-to-seeds" if seed_refs else "related-to-library"
    tagged_graph = [(p, graph_via) for p in graph_arm if _is_usable(p)]
    tagged_keyword = [
        (p, "interest-keyword") for p in keyword_arm if _is_usable(p)
    ]
    # When the caller states interests, those lead the interleave —
    # the user asked for them; library taste is the secondary signal.
    first, second = (
        (tagged_keyword, tagged_graph)
        if interests
        else (tagged_graph, tagged_keyword)
    )
    candidates: list[tuple[Paper, str]] = []
    for lead, trail in zip_longest(first, second):
        if lead is not None:
            candidates.append(lead)
        if trail is not None:
            candidates.append(trail)

    known = (
        zotero_client.known_identities() if settings().zotero_enabled else set()
    )
    fresh: list[tuple[Paper, str]] = []
    seen: set[str] = set()
    for paper, via in candidates:
        keys = _identity_keys(paper)
        if keys & known or keys & seen:
            continue
        seen |= keys
        fresh.append((paper, via))

    return {
        "picks": [
            {**_summary(paper), "via": via}
            for paper, via in fresh[:max_results]
        ],
        "problems": problems,
    }


def _interest_results(phrase: str, problems: list[str]) -> list[Paper]:
    try:
        return openalex.search(phrase, max_results=5)
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429:
            problems.append(
                f"keyword search rate-limited for {phrase!r} — that "
                "interest is uncovered; wait a minute and retry"
            )
        else:
            problems.append(
                f"keyword search failed for {phrase!r} "
                f"({type(exc).__name__}) — that interest is uncovered"
            )
        return []


def _clean_collections(
    collections: list[str] | None,
) -> tuple[list[str] | None, str]:
    """Drop empty collection names; report when any were dropped."""
    if not collections:
        return None, ""
    cleaned = [name.strip() for name in collections if name.strip()]
    note = (
        " | ignored empty collection name(s)"
        if len(cleaned) < len(collections)
        else ""
    )
    return (cleaned or None), note


def _drop_blank_refs(refs: list[str]) -> tuple[list[str], str]:
    """Drop empty/whitespace refs; report when any were dropped.

    A blank ref can never match, and letting it fall through renders
    receipts oddly ("Not found in queue: ; Other Paper").
    """
    cleaned = [ref for ref in refs if ref.strip()]
    note = "ignored empty ref(s)" if len(cleaned) < len(refs) else ""
    return cleaned, note


def _is_queue_collection(collection: str) -> bool:
    """Whether a collection name refers to the Reading Queue itself."""
    return (
        collection.strip().lower()
        == settings().reading_queue_collection.strip().lower()
    )


def _ambiguity_note(ambiguous: list[dict], verb: str) -> str:
    """Render refused-as-ambiguous refs with their consumable ids."""
    parts = [
        f"{entry['ref']!r} matches {len(entry['candidates'])} items "
        f"({', '.join(entry['candidates'])})"
        for entry in ambiguous
    ]
    return (
        f"NOT {verb} (ambiguous — ask the user which, then re-run "
        f"with that id): {'; '.join(parts)}"
    )


def _collections_ignored_note(collections: list[str] | None) -> str:
    """Flag collections requested while Zotero is unconfigured."""
    if collections and not settings().zotero_enabled:
        return " | collections ignored (Zotero is not configured)"
    return ""


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
    refs: list[str],
    force: bool = False,
    dry_run: bool = False,
    collections: list[str] | None = None,
) -> str:
    """Send papers to the e-reader by arXiv id, DOI, or title.

    ``refs`` accepts arXiv ids ('2401.12345', 'arXiv:...'), arXiv
    abs/pdf URLs, DOIs, doi.org URLs, and paper titles — the same
    ``ref`` values search and recommendation results carry. Publisher
    landing-page URLs are NOT resolvable — use the DOI or title.
    Version suffixes ('2401.12345v2') are ignored; the latest arXiv
    version is delivered.
    ``collections`` optionally files the papers into topical Zotero
    collections (created on demand) in addition to the Reading Queue —
    check list_collections and ask the user when placement is unclear.

    Refs are deduplicated, and papers already tagged as sent in Zotero
    are skipped unless force=True. Large batches are split
    automatically to fit the 25-attachment / 50 MB per-email limits.
    dry_run=True previews what would be sent, with estimated sizes,
    without downloading or delivering — use it before big sends.

    Papers without an open-access PDF are not sent; if Zotero is
    configured they are still queued (tagged no-oa-pdf) so they can be
    delivered manually later. Without Zotero there is NO cross-call
    duplicate protection — re-sending the same ref ships another copy.
    Relay the full receipt — sizes, skips, and failures — to the user.
    """
    collections, collection_note = _clean_collections(collections)
    resolved, problems = _resolve_all(refs)

    already_sent: list[Paper] = []
    already_sent_items: list[dict] = []
    if settings().zotero_enabled and not force:
        remaining = []
        for paper in resolved:
            item = zotero_client.find_item(paper)
            if item is not None and zotero_client.is_sent(item):
                already_sent.append(paper)
                already_sent_items.append(item)
            else:
                remaining.append(paper)
        resolved = remaining

    sendable = [paper for paper in resolved if paper.pdf_url]
    no_pdf = [paper for paper in resolved if not paper.pdf_url]

    if dry_run:
        lines = []
        total = 0.0
        known = 0
        unknown = 0
        for paper in sendable:
            size = resolver.probe_pdf_size(paper)
            mb = f"{size / 1e6:.1f} MB" if size else "size unknown"
            total += (size or 0) / 1e6
            known += 1 if size else 0
            unknown += 0 if size else 1
            lines.append(f"{paper.title} ({mb})")
        # Only sum the papers we could size. Folding unknowns in as 0
        # would headline a misleadingly small total — the opposite of
        # what a pre-send size check is for.
        if known:
            headline = f"Would send {len(sendable)} paper(s), ~{total:.1f} MB"
            headline += " for the ones I could size"
        else:
            headline = f"Would send {len(sendable)} paper(s)"
        if unknown:
            headline += (
                f"; {unknown} of unknown size (could push a batch over "
                "the 50 MB email limit — send those separately if unsure)"
            )
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

    # Filing is independent of delivery: papers skipped as already
    # sent still get filed into the requested collections. (Not in
    # dry_run — previews must not mutate.)
    if settings().zotero_enabled and collections:
        for item in already_sent_items:
            zotero_client.file_item(item, collections)

    downloaded, failures = _download_all(sendable)
    if failures and settings().zotero_enabled:
        # Failed downloads are still recorded in the queue below, so a
        # later send_queue retries them — say so instead of leaving
        # the failure looking final.
        failures = [
            f"{failure} — queued unsent for retry" for failure in failures
        ]
    problems.extend(failures)

    if not downloaded:
        queued_note = ""
        if settings().zotero_enabled and resolved:
            for paper in resolved:
                item_key, _ = zotero_client.add_paper(
                    paper, collections=collections
                )
                if not paper.pdf_url:
                    zotero_client.mark_no_pdf(item_key)
            queued_note = " (queued unsent in Zotero Reading Queue"
            if collections:
                queued_note += f", filed under: {'; '.join(collections)}"
            queued_note += ")"
        skips = [f"no open-access PDF: {p.title}" for p in no_pdf] + [
            "already sent (use force=True to resend"
            + (", filed into requested collections" if collections else "")
            + f"): {p.title}"
            for p in already_sent
        ]
        return (
            "Nothing was sent."
            + queued_note
            + " "
            + "; ".join(skips + problems)
            + (_oa_hint() if no_pdf else "")
            + collection_note
            + _collections_ignored_note(collections)
        )

    documents = [
        (paper.safe_filename, content) for paper, content in downloaded
    ]
    receipt, delivered = _deliver(documents)

    if settings().zotero_enabled:
        for paper in resolved:
            item_key, _ = zotero_client.add_paper(
                paper, collections=collections
            )
            if paper.safe_filename in delivered:
                zotero_client.mark_sent(item_key)
            elif not paper.pdf_url:
                zotero_client.mark_no_pdf(item_key)
        receipt += " (recorded in Zotero Reading Queue)"
        if collections:
            receipt += f" (filed under: {'; '.join(collections)})"
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
        extra = ", filed into requested collections" if collections else ""
        receipt += (
            f" | Already sent, skipped (force=True to resend{extra}): {titles}"
        )
    if problems:
        receipt += f" | Problems: {'; '.join(problems)}"
    return receipt + collection_note + _collections_ignored_note(collections)


@mcp.tool
def queue_papers(refs: list[str], collections: list[str] | None = None) -> str:
    """Add papers to the Zotero Reading Queue without sending them.

    Accepts arXiv ids, DOIs, URLs, or paper titles. Papers already in
    the queue are reported as such, not re-added. ``collections``
    optionally files the papers into topical Zotero collections
    (created on demand) as well — check list_collections and ask the
    user when placement is unclear. Unresolvable papers are reported
    back — relay those to the user.
    """
    zotero_client.ensure_configured()
    collections, collection_note = _clean_collections(collections)
    resolved, problems = _resolve_all(refs)
    new, requeued, already, no_pdf = [], [], [], []
    bucket = {"created": new, "requeued": requeued, "already_queued": already}
    for paper in resolved:
        item_key, status = zotero_client.add_paper(
            paper, collections=collections
        )
        bucket[status].append(paper.title)
        if not paper.pdf_url:
            zotero_client.mark_no_pdf(item_key)
            no_pdf.append(paper.title)
    if not resolved:
        reason = "; ".join(problems) if problems else "no valid refs given"
        return f"Nothing was queued: {reason}"
    queue = settings().reading_queue_collection
    # Lead with what actually changed: brand-new items, else re-adds,
    # else a plain no-op — never a "Queued 0 new" that reads as failure
    # when a paper was in fact put back in the queue.
    filed = f" (filed under: {'; '.join(collections)})" if collections else ""
    if new:
        headline = f"Queued {len(new)} new paper(s) in '{queue}'{filed}"
        headline += ": " + "; ".join(new)
        parts = [headline]
        if requeued:
            parts.append(
                "re-added to the queue (already in your library): "
                + "; ".join(requeued)
            )
    elif requeued:
        parts = [
            f"Re-added {len(requeued)} paper(s) to '{queue}'{filed} "
            "(already in your library): " + "; ".join(requeued)
        ]
    else:
        parts = [f"Nothing new to queue in '{queue}'"]
    if already:
        parts.append(f"already in queue: {'; '.join(already)}")
    if no_pdf:
        parts.append(
            "no open-access PDF (won't be auto-sent): "
            + "; ".join(no_pdf)
            + _oa_hint()
        )
    if problems:
        parts.append("; ".join(problems))
    return " | ".join(parts) + collection_note


@mcp.tool
def list_collections() -> list[dict]:
    """List the user's Zotero collections (name, item count, parent).

    Check this before queueing or sending new papers: if a topical
    collection clearly fits the paper, pass it via collections=[...];
    if several could fit or none do, ask the user where to file —
    never guess silently. Naming a new collection in other tools
    creates it on demand.
    """
    zotero_client.ensure_configured()
    return zotero_client.list_collections()


@mcp.tool
def file_papers(refs: list[str], collection: str) -> str:
    """File already-queued papers into a Zotero collection.

    Only papers ALREADY in the Reading Queue can be filed — to file a
    fresh paper, queue_papers it first, or pass collections=[...] to
    queue_papers to queue and file in one step. The collection is
    created on demand, but only if at least one paper matches (a call
    that files nothing leaves no empty collection behind). Items stay
    in the queue — Zotero items can live in many collections — so
    delivery state is unaffected. Refs match like remove_from_queue:
    exact arXiv id, DOI, URL, or title.
    """
    zotero_client.ensure_configured()
    if not collection.strip():
        return "Nothing filed: the collection name must be non-empty."
    if _is_queue_collection(collection):
        return (
            "Nothing filed: every paper here is already in the Reading "
            "Queue — that membership is managed by queue_papers and "
            "remove_from_queue."
        )
    refs, blank_note = _drop_blank_refs(refs)
    filed, misses, ambiguous = zotero_client.file_by_refs(refs, collection)
    # When nothing matched, the collection was intentionally not created,
    # so don't phrase the receipt as if it exists ("Filed 0 under 'X'").
    if filed:
        parts = [
            f"Filed {len(filed)} item(s) under '{collection}': "
            + "; ".join(filed)
        ]
    else:
        parts = [f"Nothing filed; '{collection}' was not created"]
    if misses:
        parts.append(
            f"Not found in queue: {'; '.join(misses)} — filing only "
            "works on queued papers, so queue_papers these first (or use "
            "queue_papers with collections=[...] to queue and file at once)"
        )
    if ambiguous:
        parts.append(_ambiguity_note(ambiguous, "filed"))
    if blank_note:
        parts.append(blank_note)
    return " | ".join(parts)


@mcp.tool
def unfile_papers(refs: list[str], collection: str) -> str:
    """Remove papers from one Zotero collection, and nothing else.

    The inverse of file_papers, for misfiled items: membership in the
    named collection is dropped, while the item, its other collections
    (including the Reading Queue), and its sent-state are untouched.
    The Reading Queue itself is refused as a target — leaving the queue
    is remove_from_queue's job, with its keep-or-trash safeguards.
    To move a paper between collections, file it into the new one and
    unfile it from the old. Refs match like remove_from_queue: exact
    arXiv id, DOI, URL, or title, against the collection's items; an
    ambiguous ref (matching several items) removes nothing.
    """
    zotero_client.ensure_configured()
    if not collection.strip():
        return "Nothing removed: the collection name must be non-empty."
    if _is_queue_collection(collection):
        return (
            "Nothing removed: taking a paper out of the Reading Queue "
            "is remove_from_queue's job (it keeps or trashes the item "
            "safely). unfile_papers only handles topical collections."
        )
    refs, blank_note = _drop_blank_refs(refs)
    try:
        removed, misses, ambiguous = zotero_client.unfile_by_refs(
            refs, collection
        )
    except ValueError as exc:
        return f"Nothing removed: {exc}"
    receipt = f"Removed {len(removed)} item(s) from '{collection}'"
    if removed:
        receipt += ": " + "; ".join(removed)
    if misses:
        receipt += f" | Not found in that collection: {'; '.join(misses)}"
    if ambiguous:
        receipt += " | " + _ambiguity_note(ambiguous, "removed")
    if blank_note:
        receipt += f" | {blank_note}"
    return receipt


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
    queue items. Items filed into other collections keep their library
    record (and sent-state) and only leave the queue; items that live
    nowhere else are moved to Zotero's Trash (restorable in the Zotero
    app for ~30 days). Trashed items no longer count for duplicate
    protection — sending those again later will deliver them again.
    Nothing is ever deleted from the e-reader, and nothing is ever
    permanently deleted from Zotero.

    A ref that matches more than one queue item (duplicate titles)
    removes nothing; the receipt lists each candidate's specific id so
    the user can choose. Relay that choice — never pick for them.
    """
    refs, blank_note = _drop_blank_refs(refs)
    removed, misses, ambiguous = zotero_client.remove_by_refs(refs)
    # Partition so each title is reported exactly once, by outcome.
    kept = [e["title"] for e in removed if e["kept_in_library"]]
    trashed = [e["title"] for e in removed if not e["kept_in_library"]]
    forgotten = [
        e["title"]
        for e in removed
        if e["was_sent"] and not e["kept_in_library"]
    ]
    receipt = f"Removed {len(removed)} item(s) from the queue"
    if kept:
        receipt += (
            " | kept in the library, still filed in other collections "
            f"(sent-state preserved): {'; '.join(kept)}"
        )
    if trashed:
        receipt += (
            " | moved to Zotero's Trash, restorable for ~30 days: "
            + "; ".join(trashed)
        )
    if forgotten:
        receipt += (
            " | note: the trashed items above no longer count for "
            "duplicate protection, so sending these again later WILL "
            f"re-deliver: {'; '.join(forgotten)}"
        )
    if misses:
        receipt += f" | Not found in queue: {'; '.join(misses)}"
    if ambiguous:
        receipt += " | " + _ambiguity_note(ambiguous, "removed")
    if blank_note:
        receipt += f" | {blank_note}"
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
    if cfg.delivery_method == "email":
        delivery_ready = email_ready
    elif cfg.delivery_method == "dropbox":
        delivery_ready = dropbox_ready
    else:
        delivery_ready = False
    next_steps = []
    if cfg.delivery_method not in ("email", "dropbox"):
        next_steps.append(
            f"DELIVERY_METHOD is {cfg.delivery_method!r} — it must be "
            "'email' or 'dropbox'."
        )
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


def _http_auth():
    """Build authentication for the HTTP transport.

    The server can send email as the user, so remote access is never
    served unauthenticated. Two modes:

    - Bearer token only (default): MCP_AUTH_TOKEN (Cloud Run: mount
      from Secret Manager) pasted into the client's Authorization
      header. Works in Claude Code and the API.
    - Bearer token + Google OAuth: additionally set
      GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
      SERVER_BASE_URL, and OAUTH_ALLOWED_EMAILS. This adds the OAuth
      flow claude.ai and the mobile app need; the bearer token keeps
      working alongside it.
    """
    token = os.environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit(
            "HTTP transport requires MCP_AUTH_TOKEN (at least 32 chars). "
            "Generate one with: python -c "
            "'import secrets; print(secrets.token_urlsafe(32))'"
        )
    from . import oauth

    if oauth.oauth_configured():
        return oauth.build_auth(static_token=token)
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
        mcp.auth = _http_auth()
        mcp.run(transport="http", host="0.0.0.0", port=int(port))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
