"""Resolve and fetch papers from arXiv.

Uses the public arXiv Atom API for metadata/search and the standard PDF
endpoint for downloads. HTML->EPUB conversion (for reflowable Kindle
reading) is on the roadmap; PDF is the lossless default for math-heavy
papers.
"""

import re
import xml.etree.ElementTree as ET

from .models import Paper
from .net import client

_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"
# New-style (2401.12345) and pre-2007 (math.GT/0309136) identifiers
_ID_PATTERN = re.compile(
    r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$"
)


def normalize_id(id_or_url: str) -> str:
    """Accept '2401.12345', 'arXiv:2401.12345v2', or an abs/pdf URL."""
    text = id_or_url.strip().removeprefix("arXiv:").removeprefix("arxiv:")
    match = _ID_PATTERN.search(text.removesuffix(".pdf"))
    if not match:
        raise ValueError(f"Could not parse arXiv id from: {id_or_url!r}")
    return match.group(1)


def _parse_entry(entry: ET.Element) -> Paper:
    raw_id = entry.findtext(f"{_ATOM}id", "")
    arxiv_id = normalize_id(raw_id)
    title = " ".join(entry.findtext(f"{_ATOM}title", "").split())
    abstract = " ".join(entry.findtext(f"{_ATOM}summary", "").split())
    authors = [
        author.findtext(f"{_ATOM}name", "")
        for author in entry.findall(f"{_ATOM}author")
    ]
    published = entry.findtext(f"{_ATOM}published", "")[:10]
    return Paper(
        title=title,
        authors=authors,
        abstract=abstract,
        published=published,
        url=f"https://arxiv.org/abs/{arxiv_id}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}",
        arxiv_id=arxiv_id,
    )


def get_paper(id_or_url: str) -> Paper:
    """Fetch metadata for one paper by arXiv id or URL."""
    arxiv_id = normalize_id(id_or_url)
    response = client.get(_API, params={"id_list": arxiv_id})
    response.raise_for_status()
    entry = ET.fromstring(response.text).find(f"{_ATOM}entry")
    if entry is None or entry.findtext(f"{_ATOM}title") is None:
        raise ValueError(f"arXiv paper not found: {arxiv_id}")
    return _parse_entry(entry)


def search(query: str, max_results: int = 5) -> list[Paper]:
    """Search arXiv by relevance and return up to ``max_results`` papers."""
    response = client.get(
        _API,
        params={
            "search_query": f"all:{query}",
            "max_results": max_results,
            "sortBy": "relevance",
        },
    )
    response.raise_for_status()
    root = ET.fromstring(response.text)
    return [_parse_entry(entry) for entry in root.findall(f"{_ATOM}entry")]
