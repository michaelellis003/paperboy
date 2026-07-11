"""Resolve DOIs via Crossref metadata and Unpaywall open-access lookup.

Crossref supplies bibliographic metadata; Unpaywall locates a legal
open-access PDF when one exists. Papers without an OA PDF still resolve
(so they can be queued in Zotero) but cannot be delivered to the Kindle.
"""

import re

from .config import settings
from .models import Paper
from .net import client

_CROSSREF = "https://api.crossref.org/works/"
_UNPAYWALL = "https://api.unpaywall.org/v2/"
_DOI_PATTERN = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
# arXiv registers DataCite DOIs under the 10.48550/arXiv.<id> prefix;
# those resolve better through the arXiv API than through Crossref.
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)
_JATS_TAG = re.compile(r"<[^>]+>")


def extract_doi(ref: str) -> str | None:
    """Pull a DOI out of a bare DOI, a 'doi:' prefix, or a doi.org URL."""
    match = _DOI_PATTERN.search(ref.strip())
    if not match:
        return None
    return match.group(1).rstrip(".,;")


def arxiv_id_from_doi(doi: str) -> str | None:
    """Return the arXiv id when the DOI is arXiv's own DataCite DOI."""
    match = _ARXIV_DOI.match(doi)
    return match.group(1) if match else None


def get_paper(doi: str) -> Paper:
    """Fetch metadata from Crossref and an OA PDF link from Unpaywall."""
    response = client.get(_CROSSREF + doi)
    if response.status_code == 404:
        raise ValueError(f"DOI not found in Crossref: {doi}")
    response.raise_for_status()
    message = response.json()["message"]

    titles = message.get("title") or []
    title = " ".join(titles[0].split()) if titles else doi
    authors = []
    for author in message.get("author", []):
        parts = (author.get("given"), author.get("family"))
        name = " ".join(p for p in parts if p) or author.get("name", "")
        if name:
            authors.append(name)
    abstract = _JATS_TAG.sub("", message.get("abstract", "")).strip()
    date_parts = (message.get("issued", {}).get("date-parts") or [[]])[0]
    published = "-".join(
        str(part) if index == 0 else f"{part:02d}"
        for index, part in enumerate(date_parts)
        if part is not None
    )

    return Paper(
        title=title,
        authors=authors,
        abstract=abstract,
        published=published,
        url=message.get("URL", f"https://doi.org/{doi}"),
        pdf_url=_oa_pdf_url(doi),
        doi=doi,
    )


def _oa_pdf_url(doi: str) -> str | None:
    """Find a direct open-access PDF link via Unpaywall, if any."""
    email = settings().polite_email
    if not email:
        # Unpaywall rejects requests without a contact email (HTTP
        # 422). Skip the doomed call; setup_status and send receipts
        # tell the user to set CONTACT_EMAIL.
        return None
    response = client.get(_UNPAYWALL + doi, params={"email": email})
    if response.status_code != 200:
        return None
    data = response.json()
    locations = [
        data.get("best_oa_location"),
        *(data.get("oa_locations") or []),
    ]
    for location in locations:
        if location and location.get("url_for_pdf"):
            return location["url_for_pdf"]
    return None
