"""Shared paper model, independent of where the paper was resolved."""

import re
import unicodedata
from dataclasses import dataclass

# Unicode-aware: [\W_] keeps letters and digits of EVERY script. An
# ASCII-only class would collapse a Cyrillic or CJK title to "", making
# all such titles false-equal across dedup and title matching.
_WORDS = re.compile(r"[\W_]")
_HTML_TAG = re.compile(r"<[^>]+>")


def normalize_title(text: str) -> str:
    """Casefolded, punctuation-free title for matching/dedup.

    NFKC unifies composed/decomposed forms (NFC "ö" vs NFD "o"+umlaut)
    so two sources disagreeing on Unicode form still deduplicate.
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(_WORDS.sub(" ", folded).split())


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


def biorxiv_pdf_urls(doi: str | None) -> list[str]:
    """Deterministic PDF URLs for a bioRxiv/medRxiv (10.1101) DOI.

    These preprint servers are open access by definition, but the OA
    index (Unpaywall/OpenAlex) has spotty coverage of them. The PDF
    lives at a DOI-derived path that redirects to the latest version.
    The two servers share the 10.1101 prefix and can't be told apart
    from the DOI, so both hosts are returned; the download path tries
    each and verifies the payload is a real PDF.
    """
    if not doi or not doi.startswith("10.1101/"):
        return []
    return [
        f"https://www.biorxiv.org/content/{doi}.full.pdf",
        f"https://www.medrxiv.org/content/{doi}.full.pdf",
    ]


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
        # bioRxiv/medRxiv preprints are open by definition; when the OA
        # index has no record, fall back to their deterministic PDF URL
        # so the paper isn't wrongly reported as having no open-access
        # PDF. Every source that omits a pdf_url gets this uniformly.
        if self.pdf_url is None:
            fallbacks = biorxiv_pdf_urls(self.doi)
            if fallbacks:
                self.pdf_url = fallbacks[0]

    @property
    def safe_filename(self) -> str:
        """Filesystem- and email-safe PDF filename derived from the title."""
        slug = re.sub(r"[^\w\s-]", "", self.title).strip()
        slug = re.sub(r"\s+", "_", slug)[:80]
        suffix = (self.arxiv_id or self.doi or "paper").replace("/", "_")
        return f"{slug}_{suffix}.pdf"
