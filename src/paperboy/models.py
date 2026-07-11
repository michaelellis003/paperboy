"""Shared paper model, independent of where the paper was resolved."""

import re
from dataclasses import dataclass

_WORDS = re.compile(r"[^a-z0-9 ]")


def normalize_title(text: str) -> str:
    """Lowercased, punctuation-free title for matching/dedup."""
    return " ".join(_WORDS.sub(" ", text.lower()).split())


@dataclass
class Paper:
    """Metadata for a resolvable paper.

    ``pdf_url`` is None when no open-access PDF was found; such papers
    can still be queued in Zotero but cannot be delivered.
    """

    title: str
    authors: list[str]
    abstract: str
    published: str
    url: str
    pdf_url: str | None
    arxiv_id: str | None = None
    doi: str | None = None

    @property
    def safe_filename(self) -> str:
        """Filesystem- and email-safe PDF filename derived from the title."""
        slug = re.sub(r"[^\w\s-]", "", self.title).strip()
        slug = re.sub(r"\s+", "_", slug)[:80]
        suffix = (self.arxiv_id or self.doi or "paper").replace("/", "_")
        return f"{slug}_{suffix}.pdf"
