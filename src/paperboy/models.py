"""Shared paper model, independent of where the paper was resolved."""

import re
from dataclasses import dataclass

_WORDS = re.compile(r"[^a-z0-9 ]")
_HTML_TAG = re.compile(r"<[^>]+>")


def normalize_title(text: str) -> str:
    """Lowercased, punctuation-free title for matching/dedup."""
    return " ".join(_WORDS.sub(" ", text.lower()).split())


def clean_title(text: str) -> str:
    """Strip inline HTML/JATS markup and collapse whitespace.

    Publisher metadata (Physical Review titles especially) carries tags
    like ``<i>Colloquium</i>`` that would otherwise show up verbatim in
    search results and receipts.
    """
    stripped = _HTML_TAG.sub("", text)
    # Tag removal can leave a space before punctuation ("Colloquium :").
    stripped = re.sub(r"\s+([:;,.])", r"\1", stripped)
    return " ".join(stripped.split())


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

    def __post_init__(self) -> None:
        # Clean the title once, here, so every source (arXiv, Crossref,
        # OpenAlex, Semantic Scholar) is covered and none can leak the
        # <i>...</i> markup that publisher metadata carries.
        self.title = clean_title(self.title)

    @property
    def safe_filename(self) -> str:
        """Filesystem- and email-safe PDF filename derived from the title."""
        slug = re.sub(r"[^\w\s-]", "", self.title).strip()
        slug = re.sub(r"\s+", "_", slug)[:80]
        suffix = (self.arxiv_id or self.doi or "paper").replace("/", "_")
        return f"{slug}_{suffix}.pdf"
