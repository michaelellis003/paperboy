"""Zotero web-API integration via pyzotero.

Zotero is the source of truth: papers land in a Reading Queue collection,
and delivery state is recorded as tags on the item — ``sent-to-ereader``
(configurable) once delivered, ``no-oa-pdf`` when no open-access PDF
could be found (so send_queue stops retrying it every run).
"""

import re
from functools import lru_cache
from typing import Any

from pyzotero import zotero, zotero_errors

from . import arxiv, doi
from .books import Book, normalize_isbn
from .config import settings
from .models import Paper, normalize_title

NO_PDF_TAG = "no-oa-pdf"


class ZoteroUnavailableError(RuntimeError):
    """Zotero could not be reached — transient, worth retrying."""


def ensure_configured() -> None:
    """Raise an actionable error when Zotero credentials are missing."""
    if not settings().zotero_enabled:
        raise RuntimeError(
            "Zotero is not configured — the reading queue requires it. "
            "Ask the user to run 'paperboy setup' in a terminal "
            "(setup_status shows what is missing)."
        )


@lru_cache(maxsize=1)
def _api() -> zotero.Zotero:
    ensure_configured()
    cfg = settings()
    return zotero.Zotero(
        cfg.zotero_library_id, cfg.zotero_library_type, cfg.zotero_api_key
    )


def _collections_raw() -> list[dict]:
    api = _api()
    return api.everything(api.collections())


def collection_key(name: str, create: bool = False) -> str | None:
    """Find a collection key by name (case-insensitive).

    With ``create=True`` a missing collection is created (top-level).
    Raises ValueError for empty names — Zotero rejects them, and a
    silently created nameless collection is worse.
    """
    wanted = name.strip()
    if not wanted:
        raise ValueError("Collection name must be non-empty.")
    for collection in _collections_raw():
        if collection["data"]["name"].lower() == wanted.lower():
            return collection["key"]
    if not create:
        return None
    result = _api().create_collections([{"name": wanted}])
    if result.get("failed"):
        raise RuntimeError(
            f"Zotero rejected collection {wanted!r}: {result['failed']}"
        )
    return result["successful"]["0"]["key"]


def list_collections() -> list[dict]:
    """Every collection with name, item count, and parent name."""
    raw = _collections_raw()
    names = {c["key"]: c["data"]["name"] for c in raw}
    return [
        {
            "name": c["data"]["name"],
            "items": c.get("meta", {}).get("numItems", 0),
            "parent": names.get(c["data"].get("parentCollection") or ""),
        }
        for c in raw
    ]


def _queue_collection_key() -> str:
    """Find or create the Reading Queue collection."""
    key = collection_key(settings().reading_queue_collection, create=True)
    assert key is not None  # create=True always yields a key
    return key


def _queue_items() -> list[dict]:
    # Read-only lookup: an absent queue collection means an empty
    # queue — creating it here would make read paths (list_queue,
    # find_item during dry_run) mutate the library.
    key = collection_key(settings().reading_queue_collection)
    if key is None:
        return []
    api = _api()
    return api.everything(api.collection_items_top(key))


def _tags(item: dict) -> set[str]:
    return {t["tag"] for t in item["data"].get("tags", [])}


def display_title(data: dict[str, Any], fallback: str) -> str:
    """Human-readable item name: non-blank title, else the fallback key.

    Externally-created items can carry a whitespace-only title, which is
    truthy — a bare ``or`` fallback would render receipts nameless.
    """
    return (data.get("title") or "").strip() or fallback


# The item types a fuzzy title match may bridge to: what this pipeline
# creates for papers, plus conferencePaper — the type Zotero's browser
# connector saves CS papers as, usually with no DOI/arXiv id, so the
# title bridge is their ONLY dedup path (excluding it re-delivers
# already-sent papers after an upgrade). Exact id matches (arXiv/DOI)
# are identity-safe across any type; a normalized-title match is not.
_PAPER_ITEM_TYPES = ("preprint", "journalArticle", "conferencePaper")


def _matches(paper: Paper, data: dict[str, Any]) -> bool:
    """Whether an existing Zotero item is the same paper."""
    if paper.arxiv_id and (
        paper.arxiv_id in data.get("url", "")
        or data.get("archiveID", "") == f"arXiv:{paper.arxiv_id}"
    ):
        return True
    doi = data.get("DOI", "")
    if paper.doi and doi and paper.doi.lower() == doi.lower():
        return True
    # DOI- and arXiv-resolved forms of the same paper can share no id
    # fields at all; the normalized title is the fallback bridge. But it
    # is fuzzy — titles collide across works and item types — so it must
    # stay WITHIN paper records. Bridging to a book, or to a report /
    # thesis / manuscript that attach_pdf can create, would merge two
    # distinct works and clobber the grey-lit item's delivery-state.
    # (Real Zotero always sets itemType; a missing one is a test fixture.)
    item_type = data.get("itemType", "")
    if item_type and item_type not in _PAPER_ITEM_TYPES:
        return False
    # Both normalized forms must be non-empty: two all-punctuation (or
    # otherwise normalization-degenerate) titles are not the same work.
    wanted = normalize_title(paper.title)
    title = data.get("title", "")
    return bool(title and wanted and wanted == normalize_title(title))


def find_item(paper: Paper) -> dict | None:
    """Return the library item matching this paper, if any.

    Checks the Reading Queue first (the common case), then falls back
    to a library-wide search — items removed from the queue but kept
    in topical collections must still deduplicate, or a re-send would
    re-deliver and create a duplicate record.
    """
    for item in _queue_items():
        if _matches(paper, item["data"]):
            return item
    api = _api()
    for query in (paper.doi, paper.arxiv_id, paper.title):
        if not query:
            continue
        for item in api.items(q=query, qmode="everything", limit=25):
            if _matches(paper, item.get("data", {})):
                return item
    return None


def is_sent(item: dict) -> bool:
    """Whether the item is already tagged as delivered."""
    return settings().sent_tag in _tags(item)


def _add_to_collection(item: dict, key: str) -> None:
    if key not in item["data"].get("collections", []):
        _api().addto_collection(key, item)


def _add_to_collections(item: dict, keys: list[str]) -> list[str]:
    """File an item into several collections with ONE write.

    pyzotero's addto_collection PATCHes a body built from the passed
    (stale) dict, guarded by If-Unmodified-Since-Version — so a second
    sequential call for the same item fails with 412 (or would lose the
    first add). Building the membership union locally and writing once
    sidesteps the conflict. Returns the post-write membership keys.
    """
    current = item["data"].get("collections", [])
    union = list(current)
    for key in keys:
        if key and key not in union:
            union.append(key)
    if union == list(current):
        return union
    version = item.get("version") or item["data"].get("version")
    _api().update_item(
        {"key": item["key"], "version": version, "collections": union}
    )
    return union


def file_item(item: dict, collections: list[str] | None) -> None:
    """File an existing item into named collections (created on demand)."""
    keys = [
        key
        for name in collections or []
        if (key := collection_key(name, create=True))
    ]
    _add_to_collections(item, keys)


def _creator_type(template: dict) -> str:
    """The default creator type for an item type ('author' as fallback).

    Read from the type's own template so an unusual item_type that wants
    'presenter' or 'editor' doesn't produce items Zotero rejects.
    """
    creators = template.get("creators") or [{}]
    return creators[0].get("creatorType") or "author"


def _base_template(
    item_type: str,
    *,
    title: str,
    authors: list[str],
    abstract: str = "",
    date: str = "",
    url: str = "",
    doi: str | None = None,
    collection_keys: list[str] | None = None,
) -> dict:
    """A Zotero item template of ``item_type`` with the common fields set.

    Shared by the paper, book, and attach-PDF item builders. ``DOI`` is
    only written when the type's template carries that field (the
    current schema has one on every type this server creates; the guard
    protects against an older cached template, where an invalid key
    would make create_items fail).
    """
    template = _api().item_template(item_type)
    template["title"] = title
    template["creators"] = [
        {"creatorType": _creator_type(template), "name": name}
        for name in authors
    ]
    template["abstractNote"] = abstract
    template["date"] = date
    template["url"] = url
    if doi and "DOI" in template:
        template["DOI"] = doi
    template["collections"] = list(collection_keys or [])
    return template


def _paper_template(paper: Paper, collection_keys: list[str]) -> dict:
    item_type = "preprint" if paper.arxiv_id else "journalArticle"
    template = _base_template(
        item_type,
        title=paper.title,
        authors=paper.authors,
        abstract=paper.abstract,
        date=paper.published,
        url=paper.url,
        doi=paper.doi,
        collection_keys=collection_keys,
    )
    if paper.arxiv_id:
        template["repository"] = "arXiv"
        template["archiveID"] = f"arXiv:{paper.arxiv_id}"
    return template


def _receipt_names(keys: list[str]) -> list[str]:
    """Best-effort collection names for a receipt.

    Runs AFTER the library was mutated, so a Zotero hiccup here must
    degrade to an unadorned receipt — never destroy it (the caller would
    retry and re-mutate).
    """
    try:
        return _names_for_keys(keys)
    except Exception:
        return []


def _upsert_paper(
    paper: Paper, collections: list[str] | None, queue: bool
) -> tuple[str, str, list[str]]:
    """Create-or-find a paper item and file it; optionally into the queue.

    Returns (item key, status, collection names). With ``queue`` the
    status is created/requeued/already_queued and the names are [] (the
    queue receipts don't use them — computing them would cost an extra
    post-mutation API call per paper); without it (catalog-only) status
    is created/existing and the names are the item's membership AFTER
    filing, so a duplicate receipt can say where the paper lives.
    """
    api = _api()
    extra_keys = [
        collection_key(name, create=True) for name in collections or []
    ]
    queue_key = _queue_collection_key() if queue else None

    existing = find_item(paper)
    if existing:
        was_queued = bool(
            queue_key and queue_key in existing["data"].get("collections", [])
        )
        # One write for all additions (see _add_to_collections); it
        # returns the post-filing membership, which pyzotero would not
        # reflect in the passed dict.
        member_keys = _add_to_collections(
            existing,
            [k for k in [queue_key, *extra_keys] if k],
        )
        if not queue:
            return existing["key"], "existing", _receipt_names(member_keys)
        # Distinguish a true no-op (already queued) from an item pulled
        # back into the queue, so the receipt is honest.
        status = "already_queued" if was_queued else "requeued"
        return existing["key"], status, []

    base_keys = ([queue_key] if queue_key else []) + [
        k for k in extra_keys if k
    ]
    result = api.create_items([_paper_template(paper, base_keys)])
    if result["failed"]:
        raise RuntimeError(f"Zotero rejected item: {result['failed']}")
    key = result["successful"]["0"]["key"]
    return key, "created", ([] if queue else _receipt_names(base_keys))


def add_paper(
    paper: Paper, collections: list[str] | None = None
) -> tuple[str, str]:
    """Add a paper to the Reading Queue, deduplicating by arXiv id/DOI.

    arXiv papers become ``preprint`` items; DOI-resolved papers become
    ``journalArticle`` items. ``collections`` names extra topical
    collections to file into (created on demand) — Zotero items can
    live in many collections, so the queue membership is unaffected.

    Returns (item key, status) where status is:
    - "created": a new library item was created and queued;
    - "requeued": the paper existed in the library (e.g. filed in a
      collection) but was not in the queue, and was re-added to it;
    - "already_queued": the paper was already in the queue (no change).
    """
    key, status, _ = _upsert_paper(paper, collections, queue=True)
    return key, status


def catalog_paper(
    paper: Paper, collections: list[str] | None = None
) -> tuple[str, str, list[str]]:
    """Add a paper to the library WITHOUT queueing it for delivery.

    The "just track it" path: create-or-dedup the item and file it into
    ``collections``, never touching the Reading Queue and never tagging
    no-oa-pdf. A paper with no open-access PDF is a perfectly good
    library record here, not a delivery failure. Returns (item key,
    status, collection names) where status is "created" or "existing".
    """
    return _upsert_paper(paper, collections, queue=False)


def _book_isbns(data: dict[str, Any]) -> set[str]:
    """Every valid ISBN in the item's ISBN field, normalized to ISBN-13.

    Zotero's field is free text: translators (MARC especially) store
    several ISBNs space- or comma-separated (hbk/pbk/ebook of one
    record), and single ISBNs are sometimes written with internal
    spaces ("978 0 306 40615 7"). So the whole field is tried first,
    then each token — and ALL results count, because the incoming ISBN
    may match any of the stored ones.
    """
    raw = data.get("ISBN", "")
    if not raw:
        return set()
    isbns = set()
    whole = normalize_isbn(raw)
    if whole:
        isbns.add(whole)
    for token in re.split(r"[,;\s]+", raw):
        if token:
            isbn = normalize_isbn(token)
            if isbn:
                isbns.add(isbn)
    return isbns


def _matches_book(book: Book, data: dict[str, Any]) -> bool:
    """Whether an existing item is the same book.

    ISBN-first: when either side carries an ISBN, only equal ISBNs
    match. Title matching is a fallback used only when NEITHER side has
    an ISBN — distinct editions legitimately share a title and are
    separate works worth keeping apart.
    """
    if data.get("itemType") != "book":
        return False
    stored_isbns = _book_isbns(data)
    if book.isbn or stored_isbns:
        return bool(book.isbn and book.isbn in stored_isbns)
    wanted = normalize_title(book.title)
    title = data.get("title", "")
    return bool(title and wanted and wanted == normalize_title(title))


def find_book(book: Book) -> dict | None:
    """Return the library book matching this one, if any (no queue scan)."""
    api = _api()
    for query in (book.isbn, book.title):
        if not query:
            continue
        for item in api.items(q=query, qmode="everything", limit=25):
            if _matches_book(book, item.get("data", {})):
                return item
    return None


def _book_template(book: Book, collection_keys: list[str]) -> dict:
    # Zotero's current schema gives ``book`` a real DOI field, so the
    # DOI travels through _base_template like any other type's. (Older
    # schemas kept it in Extra; the web API always serves the current
    # one.)
    template = _base_template(
        "book",
        title=book.title,
        authors=book.authors,
        abstract=book.abstract,
        date=book.year,
        url=book.url,
        doi=book.doi,
        collection_keys=collection_keys,
    )
    for field, value in (
        ("publisher", book.publisher),
        ("edition", book.edition),
        ("series", book.series),
        ("numPages", book.num_pages),
        ("place", book.place),
        ("ISBN", book.isbn),
    ):
        if value:
            template[field] = value
    # Belt-and-suspenders for a template without the DOI field (an old
    # cached schema): the DOI must land somewhere searchable.
    if book.doi and "DOI" not in template:
        template["extra"] = f"DOI: {book.doi}"
    return template


def add_book(
    book: Book, collections: list[str] | None = None
) -> tuple[str, str, list[str]]:
    """Catalog a book as a Zotero ``book`` item, deduplicating by ISBN.

    Never queues and never delivers — books are cataloged, not sent (an
    open-access textbook PDF goes through attach_pdf). Returns (item
    key, status, collection names) where status is "created" or
    "existing".
    """
    api = _api()
    extra_keys = [
        collection_key(name, create=True) for name in collections or []
    ]
    kept_keys = [k for k in extra_keys if k]
    existing = find_book(book)
    if existing:
        # One write for all additions; returns post-filing membership
        # (pyzotero would not reflect it in the passed dict).
        member_keys = _add_to_collections(existing, kept_keys)
        return existing["key"], "existing", _receipt_names(member_keys)
    result = api.create_items([_book_template(book, kept_keys)])
    if result["failed"]:
        raise RuntimeError(f"Zotero rejected book: {result['failed']}")
    return (
        result["successful"]["0"]["key"],
        "created",
        _receipt_names(kept_keys),
    )


def _attach_file(parent_key: str, pdf_path: str) -> bool:
    """Upload a local PDF as a child attachment; True when it stuck.

    An exception here must NOT propagate: the parent item was already
    created, and a raised upload failure escaping to the tool's
    create-item error handler would produce a false "could not create
    the item" receipt — whose natural retry then duplicates the item.
    A failed upload is a fact about the attachment, reported as False.
    """
    try:
        result = _api().attachment_simple([pdf_path], parentid=parent_key)
    except Exception:
        return False
    return not result.get("failure")


def _has_pdf_attachment(item_key: str) -> bool:
    """Whether the item already has a PDF child attachment.

    Best-effort: when children can't be listed, answer False and let
    the upload proceed — a duplicate attachment is the lesser evil to
    a missing one.
    """
    try:
        children = _api().children(item_key)
    except Exception:
        return False
    return any(
        child.get("data", {}).get("contentType") == "application/pdf"
        for child in children
    )


def find_attach_target(item_type: str, title: str) -> dict | None:
    """An existing item with this exact type and normalized title.

    Makes attach_pdf retry-safe: re-running after a failed upload (or a
    lost receipt) attaches to the item already created instead of
    minting a duplicate that nothing would ever dedup.
    """
    api = _api()
    wanted = normalize_title(title)
    if not wanted:
        # A normalization-degenerate title (all punctuation/symbols)
        # must never reuse-by-title — it would match any other such item.
        return None
    # titleCreatorYear mode searches titles rather than full text, so a
    # generic title can't drown the target past the fetch limit (a miss
    # here degrades to a duplicate item — the exact failure reuse is
    # supposed to prevent).
    for item in api.items(q=title, qmode="titleCreatorYear", limit=50):
        data = item.get("data", {})
        if data.get("itemType") == item_type and wanted == normalize_title(
            data.get("title", "")
        ):
            return item
    return None


def attach_pdf_item(
    *,
    item_type: str,
    title: str,
    authors: list[str],
    pdf_path: str,
    year: str = "",
    url: str = "",
    doi: str | None = None,
    abstract: str = "",
    collections: list[str] | None = None,
) -> tuple[str, str, bool]:
    """Create-or-find an item of ``item_type`` and attach a local PDF.

    The grey-literature path: caller-supplied metadata, no registry
    lookup, no queue. An existing item with the same type and exact
    normalized title is reused (filed into any new collections) so a
    retry after a failed upload never duplicates the item. Returns
    (item key, "created" | "existing", whether the PDF attached).
    """
    api = _api()
    extra_keys = [
        collection_key(name, create=True) for name in collections or []
    ]
    kept_keys = [k for k in extra_keys if k]

    existing = find_attach_target(item_type, title)
    if existing:
        _add_to_collections(existing, kept_keys)
        # A successful earlier run already attached the PDF; uploading
        # again would add a duplicate child attachment (pyzotero creates
        # a new attachment item per call even when the blob dedupes).
        if _has_pdf_attachment(existing["key"]):
            return existing["key"], "existing", True
        return (
            existing["key"],
            "existing",
            _attach_file(existing["key"], pdf_path),
        )

    template = _base_template(
        item_type,
        title=title,
        authors=authors,
        abstract=abstract,
        date=year,
        url=url,
        doi=doi,
        collection_keys=kept_keys,
    )
    result = api.create_items([template])
    if result["failed"]:
        raise RuntimeError(f"Zotero rejected item: {result['failed']}")
    key = result["successful"]["0"]["key"]
    return key, "created", _attach_file(key, pdf_path)


def _names_for_keys(keys: list[str]) -> list[str]:
    if not keys:
        return []
    names = {c["key"]: c["data"]["name"] for c in _collections_raw()}
    return [names[key] for key in keys if key in names]


def item_collection_names(item: dict) -> list[str]:
    """Names of the collections an item currently belongs to."""
    return _names_for_keys(item["data"].get("collections", []))


_ITEM_KEY = re.compile(r"^[A-Z0-9]{8}$")


def scholarly_ref_for_key(ref: str) -> str | None:
    """Translate a Zotero item key into the item's best scholarly ref.

    send_papers/queue_papers resolve refs against the scholarly
    backends, which know nothing about Zotero keys — this bridge keeps
    the promise that a key from list_queue works in every ref-taking
    tool. Returns None when the ref isn't key-shaped, Zotero is off,
    or no such item exists (so ordinary resolution proceeds). Raises
    ZoteroUnavailableError when Zotero itself can't be reached: a valid key
    must not read as "could not resolve" during a transient outage.
    """
    candidate = ref.strip().upper()
    if not _ITEM_KEY.fullmatch(candidate):
        return None
    if not settings().zotero_enabled:
        return None
    try:
        item = _api().item(candidate)
    except zotero_errors.ResourceNotFoundError:
        return None  # genuinely no such item — ordinary resolution
    except Exception as exc:
        raise ZoteroUnavailableError(
            f"Zotero is temporarily unreachable while looking up item "
            f"key {candidate} — retry"
        ) from exc
    data = item.get("data", {})
    archive = data.get("archiveID", "")
    if archive.startswith("arXiv:"):
        return archive.removeprefix("arXiv:")
    return data.get("DOI") or data.get("title") or None


def _matching_items(ref: str, items: list[dict]) -> list[dict]:
    """All items a ref matches: exact ids/titles, or the Zotero item key.

    Item keys are matched so the disambiguation ids that receipts and
    list_queue advertise are actually consumable — an instruction like
    "re-run with that id" must never be a dead end.
    """
    needle = ref.strip()
    if not needle:
        return []
    key_form = needle.upper()  # keys are uppercase; accept any casing
    return [
        item
        for item in items
        if item.get("key") == key_form or _ref_matches(ref, item["data"])
    ]


def _ambiguity(ref: str, matches: list[dict]) -> dict:
    """Describe an ambiguous ref with per-candidate consumable keys.

    Every candidate carries the Zotero item key: exact duplicates (two
    items with the same arXiv id) share every other identifier, so the
    key is the only id guaranteed to single one out.
    """
    return {
        "ref": ref,
        "candidates": [
            {
                "key": m["key"],
                "id": _unique_ref(m["data"]),
                # Exact duplicates share every id; the added date is
                # often the only human-tellable difference.
                "added": m["data"].get("dateAdded", "")[:10],
            }
            for m in matches
        ],
    }


def file_by_refs(
    refs: list[str], collection_name: str
) -> tuple[list[str], list[str], list[dict]]:
    """File queue items into a collection (created on demand).

    Matching is the same exact-only logic as removal, including the
    refusal to act on a ref that matches more than one item. The
    collection is created only once at least one ref matches — a call
    that files nothing leaves no phantom empty collection behind.
    Returns (filed titles, refs that matched nothing, ambiguous refs
    with candidates).
    """
    items = _queue_items()
    filed, misses, ambiguous = [], [], []
    key: str | None = None
    done: set[str] = set()
    for ref in refs:
        matches = _matching_items(ref, items)
        if not matches:
            misses.append(ref)
            continue
        # Two refs naming the same item (its DOI and its title, say):
        # the item is already filed, and a second stale-version write
        # would 412 against the real API. Consume the ref only when
        # EVERYTHING it names was already handled — a ref that also
        # matches a fresh item is still genuinely ambiguous, and acting
        # on the leftover candidate would be guessing (the docstring's
        # refusal contract). Candidates list ALL matches so the hints
        # stay actionable.
        fresh = [m for m in matches if m["key"] not in done]
        if not fresh:
            continue
        if len(matches) > 1:
            ambiguous.append(_ambiguity(ref, matches))
            continue
        if key is None:
            key = collection_key(collection_name, create=True)
        if key:
            _add_to_collection(fresh[0], key)
        done.add(fresh[0]["key"])
        filed.append(display_title(fresh[0]["data"], fresh[0]["key"]))
    return filed, misses, ambiguous


def unfile_by_refs(
    refs: list[str], collection_name: str
) -> tuple[list[str], list[str], list[dict]]:
    """Remove items from one collection without touching anything else.

    The inverse of file_by_refs: membership in the named collection is
    dropped; the item itself, its other collections (including the
    Reading Queue), and its sent-state all stay as they are. Matching
    is the same exact-only logic as removal, against the collection's
    own items, and a ref matching more than one item removes nothing.
    Returns (removed titles, refs that matched nothing, ambiguous refs
    with candidates). Raises ValueError when the collection does not
    exist.
    """
    key = collection_key(collection_name)
    if key is None:
        raise ValueError(f"No collection named {collection_name!r}.")
    api = _api()
    items = api.everything(api.collection_items_top(key))
    removed, misses, ambiguous = [], [], []
    done: set[str] = set()
    for ref in refs:
        matches = _matching_items(ref, items)
        if not matches:
            misses.append(ref)
            continue
        # A second ref naming an item already removed this call would
        # re-PATCH with a stale version (412) — consume it, but only
        # when EVERYTHING it names was handled; a ref that also matches
        # a fresh item is still ambiguous (see file_by_refs).
        fresh = [m for m in matches if m["key"] not in done]
        if not fresh:
            continue
        if len(matches) > 1:
            ambiguous.append(_ambiguity(ref, matches))
            continue
        api.deletefrom_collection(key, fresh[0])
        done.add(fresh[0]["key"])
        removed.append(display_title(fresh[0]["data"], fresh[0]["key"]))
    return removed, misses, ambiguous


def seed_ids(limit: int = 10) -> list[str]:
    """S2-style seed ids (ArXiv:/DOI: prefixed) from the queue.

    Explicitly sorted newest-added first — recent additions weight
    recommendations toward the user's current focus. (The Web API's
    default sort is dateModified desc, so relying on response order
    or reversing it would seed from stale or tag-edit-perturbed
    items.)
    """
    key = collection_key(settings().reading_queue_collection)
    if key is None:
        return []
    api = _api()
    items = api.collection_items_top(
        key, sort="dateAdded", direction="desc", limit=50
    )
    ids: list[str] = []
    for item in items:
        data = item["data"]
        archive = data.get("archiveID", "")
        if archive.startswith("arXiv:"):
            ids.append(archive.replace("arXiv:", "ArXiv:", 1))
        elif data.get("DOI"):
            ids.append(f"DOI:{data['DOI']}")
        if len(ids) >= limit:
            break
    return ids


def known_identities() -> set[str]:
    """Identity keys of the user's library, for recommendation dedup.

    Lowered DOIs, arXiv ids, and normalized titles from the queue PLUS
    the 100 most recently added library items — items archived out of
    the queue into topical collections must not be re-recommended.
    """
    api = _api()
    items = list(_queue_items())
    items += api.top(sort="dateAdded", direction="desc", limit=100)
    keys: set[str] = set()
    for item in items:
        data = item.get("data", {})
        if data.get("DOI"):
            keys.add(data["DOI"].lower())
        archive = data.get("archiveID", "")
        if archive.startswith("arXiv:"):
            keys.add(archive.removeprefix("arXiv:"))
        if data.get("title"):
            normalized = normalize_title(data["title"])
            if normalized:
                # An empty normalized form would false-match every
                # equally degenerate title in the dedup intersection.
                keys.add(normalized)
    return keys


def unsent_queue_items() -> list[dict]:
    """Deliverable Reading Queue items: not sent, not known-unsendable."""
    cfg = settings()
    return [
        item
        for item in _queue_items()
        if not {cfg.sent_tag, NO_PDF_TAG} & _tags(item)
    ]


def list_queue() -> list[dict]:
    """Every top-level queue item with its delivery status."""
    cfg = settings()
    entries = []
    for item in _queue_items():
        tags = _tags(item)
        data = item["data"]
        if cfg.sent_tag in tags:
            status = "sent"
        elif NO_PDF_TAG in tags:
            status = "no-open-access-pdf"
        else:
            status = "unsent"
        entries.append(
            {
                "title": display_title(data, item["key"]),
                "ref": data.get("DOI") or data.get("url") or item["key"],
                # The item key is the only id guaranteed unique when the
                # library holds exact duplicates; every tool accepts it.
                "key": item["key"],
                "status": status,
                "added": data.get("dateAdded", "")[:10],
            }
        )
    return entries


def _ref_matches(ref: str, data: dict[str, Any]) -> bool:
    """Exact-only matching — deletion must never hit the wrong paper."""
    needle = ref.strip()
    if not needle:
        return False
    lowered = needle.lower()
    if lowered == data.get("title", "").lower():
        return True
    if lowered == data.get("url", "").lower():
        return True
    found_doi = doi.extract_doi(needle)
    if found_doi and found_doi.lower() == data.get("DOI", "").lower():
        return True
    try:
        arxiv_id = arxiv.normalize_id(needle)
    except ValueError:
        return False
    return data.get("archiveID", "") == f"arXiv:{arxiv_id}" or data.get(
        "url", ""
    ).endswith(f"/abs/{arxiv_id}")


def _trash_item(api: zotero.Zotero, item: dict) -> None:
    """Move an item to Zotero's Trash (restorable), not hard-delete it.

    A minimal PATCH of ``deleted: 1`` with the item's version is what
    the Zotero API accepts to trash an item. pyzotero's write validator
    rejects ``deleted`` by default, so widen its allow-list first; the
    version gives optimistic-locking on the update.
    """
    api.temp_keys = set(api.temp_keys) | {"deleted"}
    version = item.get("version") or item["data"].get("version")
    api.update_item({"key": item["key"], "version": version, "deleted": 1})


def _unique_ref(data: dict[str, Any]) -> str:
    """The most recognizable id for an item, for disambiguation hints."""
    archive = data.get("archiveID", "")
    if archive.startswith("arXiv:"):
        return archive
    return data.get("DOI") or data.get("url") or "no other id"


def remove_by_refs(
    refs: list[str],
) -> tuple[list[dict], list[str], list[dict]]:
    """Remove queue items matching each ref (id, DOI, URL, or title).

    Items also filed elsewhere just leave the queue; items that live
    nowhere else are moved to Zotero's Trash (restorable in the app).
    Matching is exact (normalized ids/DOIs, case-insensitive titles);
    partial or empty refs never match. A ref matching MORE than one
    item (two queue entries sharing a title) removes nothing — this is
    the one place a wrong match loses state, so ambiguity stops the
    removal instead of guessing. Returns (removed entries with
    ``title`` and ``was_sent``, refs that matched nothing, ambiguous
    refs with their candidates' specific ids).
    """
    api = _api()
    # Read-only lookup: removal from a queue that doesn't exist is a
    # no-op, and must not create the collection as a side effect.
    queue_key = collection_key(settings().reading_queue_collection)
    if queue_key is None:
        return [], [ref for ref in refs if ref.strip()], []
    items = _queue_items()
    removed, misses, ambiguous = [], [], []
    done: set[str] = set()
    for ref in refs:
        matches = _matching_items(ref, items)
        if not matches:
            misses.append(ref)
            continue
        # A later ref naming an item already removed this call (its DOI
        # and its title, say) is consumed — but only when EVERYTHING it
        # names was handled. A ref that also matches a fresh item stays
        # ambiguous: removal is the one place a wrong guess loses state,
        # so it must never act on the leftover candidate by elimination.
        fresh = [m for m in matches if m["key"] not in done]
        if not fresh:
            continue
        if len(matches) > 1:
            ambiguous.append(_ambiguity(ref, matches))
            continue
        match = fresh[0]
        # An item the user filed into topical collections is theirs to
        # keep: only drop its queue membership. Items that live nowhere
        # else go to Zotero's Trash — same as Delete in the Zotero app,
        # restorable for ~30 days — never permanent deletion.
        other = [
            key
            for key in match["data"].get("collections", [])
            if key != queue_key
        ]
        if other:
            api.deletefrom_collection(queue_key, match)
        else:
            _trash_item(api, match)
        done.add(match["key"])
        removed.append(
            {
                "title": display_title(match["data"], match["key"]),
                "was_sent": is_sent(match),
                "kept_in_library": bool(other),
            }
        )
    return removed, misses, ambiguous


def _add_tag(item_key: str, tag: str) -> None:
    api = _api()
    item = api.item(item_key)
    if tag not in {t["tag"] for t in item["data"].get("tags", [])}:
        api.add_tags(item, tag)


def mark_sent(item_key: str) -> None:
    """Tag a Zotero item as delivered (idempotent)."""
    _add_tag(item_key, settings().sent_tag)


def mark_no_pdf(item_key: str) -> None:
    """Tag an item as having no open-access PDF (idempotent)."""
    _add_tag(item_key, NO_PDF_TAG)
