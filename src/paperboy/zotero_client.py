"""Zotero web-API integration via pyzotero.

Zotero is the source of truth: papers land in a Reading Queue collection,
and delivery state is recorded as a tag on the item (default
``sent-to-kindle``) rather than in this service.
"""

from functools import lru_cache
from typing import Any

from pyzotero import zotero

from .config import settings
from .models import Paper


@lru_cache(maxsize=1)
def _api() -> zotero.Zotero:
    cfg = settings()
    if not cfg.zotero_enabled:
        raise RuntimeError(
            "Zotero is not configured. "
            "Set ZOTERO_API_KEY and ZOTERO_LIBRARY_ID."
        )
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


def _matches(paper: Paper, data: dict[str, Any]) -> bool:
    """Whether an existing Zotero item is the same paper."""
    if paper.arxiv_id and paper.arxiv_id in data.get("url", ""):
        return True
    doi = data.get("DOI", "")
    return bool(paper.doi and doi and paper.doi.lower() == doi.lower())


def add_paper(paper: Paper) -> str:
    """Add a paper to the Reading Queue, deduplicating by arXiv id/DOI.

    arXiv papers become ``preprint`` items; DOI-resolved papers become
    ``journalArticle`` items. Returns the (new or existing) item key.
    """
    api = _api()
    collection_key = _queue_collection_key()

    for item in api.everything(api.collection_items_top(collection_key)):
        if _matches(paper, item["data"]):
            return item["key"]

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
    return result["successful"]["0"]["key"]


def unsent_queue_items() -> list[dict]:
    """Top-level Reading Queue items not yet tagged as sent."""
    cfg = settings()
    api = _api()
    items = api.everything(api.collection_items_top(_queue_collection_key()))
    return [
        item
        for item in items
        if cfg.sent_tag not in {t["tag"] for t in item["data"].get("tags", [])}
    ]


def mark_sent(item_key: str) -> None:
    """Tag a Zotero item as delivered to the Kindle."""
    api = _api()
    item = api.item(item_key)
    api.add_tags(item, settings().sent_tag)
