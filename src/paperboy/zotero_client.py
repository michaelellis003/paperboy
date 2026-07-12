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
    # fields at all; the (normalized) title is the fallback bridge.
    title = data.get("title", "")
    return bool(
        title and normalize_title(paper.title) == normalize_title(title)
    )


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


def file_item(item: dict, collections: list[str] | None) -> None:
    """File an existing item into named collections (created on demand)."""
    for name in collections or []:
        key = collection_key(name, create=True)
        if key:
            _add_to_collection(item, key)


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
    api = _api()
    queue_key = _queue_collection_key()
    extra_keys = [
        collection_key(name, create=True) for name in collections or []
    ]

    existing = find_item(paper)
    if existing:
        # An item previously removed from the queue (but kept in the
        # library via topical collections) rejoins the queue instead of
        # spawning a duplicate record. Distinguish a true no-op (already
        # in the queue) from a genuine re-add so the receipt is honest.
        was_queued = queue_key in existing["data"].get("collections", [])
        _add_to_collection(existing, queue_key)
        for key in extra_keys:
            if key:
                _add_to_collection(existing, key)
        return existing["key"], ("already_queued" if was_queued else "requeued")

    if paper.arxiv_id:
        template = api.item_template("preprint")
        template["repository"] = "arXiv"
        template["archiveID"] = f"arXiv:{paper.arxiv_id}"
    else:
        template = api.item_template("journalArticle")
    template["title"] = paper.title
    template["creators"] = [
        {"creatorType": "author", "name": name} for name in paper.authors
    ]
    template["abstractNote"] = paper.abstract
    template["date"] = paper.published
    template["url"] = paper.url
    if paper.doi:
        template["DOI"] = paper.doi
    template["collections"] = [queue_key, *[k for k in extra_keys if k]]

    result = api.create_items([template])
    if result["failed"]:
        raise RuntimeError(f"Zotero rejected item: {result['failed']}")
    return result["successful"]["0"]["key"], "created"


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
    for ref in refs:
        matches = _matching_items(ref, items)
        if not matches:
            misses.append(ref)
            continue
        if len(matches) > 1:
            ambiguous.append(_ambiguity(ref, matches))
            continue
        if key is None:
            key = collection_key(collection_name, create=True)
        if key:
            _add_to_collection(matches[0], key)
        filed.append(matches[0]["data"].get("title", matches[0]["key"]))
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
    for ref in refs:
        matches = _matching_items(ref, items)
        if not matches:
            misses.append(ref)
            continue
        if len(matches) > 1:
            ambiguous.append(_ambiguity(ref, matches))
            continue
        api.deletefrom_collection(key, matches[0])
        removed.append(matches[0]["data"].get("title", matches[0]["key"]))
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
            keys.add(normalize_title(data["title"]))
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
                "title": data.get("title", item["key"]),
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
    queue_key = _queue_collection_key()
    items = _queue_items()
    removed, misses, ambiguous = [], [], []
    for ref in refs:
        matches = _matching_items(ref, items)
        if not matches:
            misses.append(ref)
            continue
        if len(matches) > 1:
            ambiguous.append(_ambiguity(ref, matches))
            continue
        match = matches[0]
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
        items.remove(match)
        removed.append(
            {
                "title": match["data"].get("title", match["key"]),
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
