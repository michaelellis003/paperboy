"""Zotero web-API integration via pyzotero.

Zotero is the source of truth: papers land in a Reading Queue collection,
and delivery state is recorded as tags on the item — ``sent-to-ereader``
(configurable) once delivered, ``no-oa-pdf`` when no open-access PDF
could be found (so send_queue stops retrying it every run).
"""

from functools import lru_cache
from typing import Any

from pyzotero import zotero

from . import arxiv, doi
from .config import settings
from .models import Paper, normalize_title

NO_PDF_TAG = "no-oa-pdf"


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
) -> tuple[str, bool]:
    """Add a paper to the Reading Queue, deduplicating by arXiv id/DOI.

    arXiv papers become ``preprint`` items; DOI-resolved papers become
    ``journalArticle`` items. ``collections`` names extra topical
    collections to file into (created on demand) — Zotero items can
    live in many collections, so the queue membership is unaffected.
    Returns (item key, created) where created is False when the paper
    was already in the queue.
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
        # spawning a duplicate record.
        _add_to_collection(existing, queue_key)
        for key in extra_keys:
            if key:
                _add_to_collection(existing, key)
        return existing["key"], False

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
    return result["successful"]["0"]["key"], True


def file_by_refs(
    refs: list[str], collection_name: str
) -> tuple[list[str], list[str]]:
    """File queue items into a collection (created on demand).

    Matching is the same exact-only logic as removal. Returns (filed
    titles, refs that matched nothing).
    """
    key = collection_key(collection_name, create=True)
    items = _queue_items()
    filed, misses = [], []
    for ref in refs:
        match = next(
            (item for item in items if _ref_matches(ref, item["data"])),
            None,
        )
        if match is None:
            misses.append(ref)
            continue
        if key:
            _add_to_collection(match, key)
        filed.append(match["data"].get("title", match["key"]))
    return filed, misses


def unfile_by_refs(
    refs: list[str], collection_name: str
) -> tuple[list[str], list[str]]:
    """Remove items from one collection without touching anything else.

    The inverse of file_by_refs: membership in the named collection is
    dropped; the item itself, its other collections (including the
    Reading Queue), and its sent-state all stay as they are. Matching
    is the same exact-only logic as removal, against the collection's
    own items. Returns (removed titles, refs that matched nothing).
    Raises ValueError when the collection does not exist.
    """
    key = collection_key(collection_name)
    if key is None:
        raise ValueError(f"No collection named {collection_name!r}.")
    api = _api()
    items = api.everything(api.collection_items_top(key))
    removed, misses = [], []
    for ref in refs:
        match = next(
            (item for item in items if _ref_matches(ref, item["data"])),
            None,
        )
        if match is None:
            misses.append(ref)
            continue
        api.deletefrom_collection(key, match)
        removed.append(match["data"].get("title", match["key"]))
    return removed, misses


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


def _unique_ref(data: dict[str, Any]) -> str:
    """The most specific ref for an item, for disambiguation hints."""
    archive = data.get("archiveID", "")
    if archive.startswith("arXiv:"):
        return archive
    return data.get("DOI") or data.get("url") or data.get("key", "?")


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
        matches = [item for item in items if _ref_matches(ref, item["data"])]
        if not matches:
            misses.append(ref)
            continue
        if len(matches) > 1:
            ambiguous.append(
                {
                    "ref": ref,
                    "candidates": [
                        _unique_ref({**m["data"], "key": m["key"]})
                        for m in matches
                    ],
                }
            )
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
            match["data"]["deleted"] = 1
            api.update_item(match)
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
