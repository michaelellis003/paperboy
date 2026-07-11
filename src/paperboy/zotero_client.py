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


def _queue_collection_key() -> str:
    """Find or create the Reading Queue collection."""
    cfg = settings()
    api = _api()
    for collection in api.everything(api.collections()):
        if collection["data"]["name"] == cfg.reading_queue_collection:
            return collection["key"]
    result = api.create_collections([{"name": cfg.reading_queue_collection}])
    return result["successful"]["0"]["key"]


def _queue_items() -> list[dict]:
    api = _api()
    return api.everything(api.collection_items_top(_queue_collection_key()))


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
    """Return the queue item matching this paper, if any."""
    for item in _queue_items():
        if _matches(paper, item["data"]):
            return item
    return None


def is_sent(item: dict) -> bool:
    """Whether the item is already tagged as delivered."""
    return settings().sent_tag in _tags(item)


def add_paper(paper: Paper) -> tuple[str, bool]:
    """Add a paper to the Reading Queue, deduplicating by arXiv id/DOI.

    arXiv papers become ``preprint`` items; DOI-resolved papers become
    ``journalArticle`` items. Returns (item key, created) where created
    is False when the paper was already in the queue.
    """
    api = _api()
    collection_key = _queue_collection_key()

    existing = find_item(paper)
    if existing:
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
    template["collections"] = [collection_key]

    result = api.create_items([template])
    if result["failed"]:
        raise RuntimeError(f"Zotero rejected item: {result['failed']}")
    return result["successful"]["0"]["key"], True


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


def remove_by_refs(refs: list[str]) -> tuple[list[str], list[str]]:
    """Delete queue items matching each ref (id, DOI, URL, or title).

    Matching is exact (normalized ids/DOIs, case-insensitive titles);
    partial or empty refs never match. Returns (removed titles, refs
    that matched nothing).
    """
    api = _api()
    items = _queue_items()
    removed, misses = [], []
    for ref in refs:
        match = next(
            (item for item in items if _ref_matches(ref, item["data"])),
            None,
        )
        if match is None:
            misses.append(ref)
            continue
        api.delete_item(match)
        items.remove(match)
        removed.append(match["data"].get("title", match["key"]))
    return removed, misses


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
