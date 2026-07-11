"""Turn user-supplied references into Papers and fetch their PDFs.

Accepts arXiv ids ('2401.12345', 'arXiv:...', abs/pdf URLs), DOIs
('10.1038/...', 'doi:...', doi.org URLs), and paper titles, routing
each to the right backend. arXiv's own DataCite DOIs are routed to the
arXiv API. Titles resolve through OpenAlex search, accepted only when
the best hit closely matches the requested title — a wrong paper on
the e-reader is worse than a lookup failure.
"""

import difflib
import re

import httpx

from . import arxiv, doi, openalex
from .models import Paper
from .net import client

_TITLE_MATCH_THRESHOLD = 0.8
_WORDS = re.compile(r"[^a-z0-9 ]")


def _normalize(text: str) -> str:
    return " ".join(_WORDS.sub(" ", text.lower()).split())


def _resolve_title(ref: str) -> Paper | None:
    matches = openalex.search(ref, max_results=1)
    if not matches:
        return None
    hit = matches[0]
    ratio = difflib.SequenceMatcher(
        None, _normalize(ref), _normalize(hit.title)
    ).ratio()
    if ratio < _TITLE_MATCH_THRESHOLD:
        return None
    # OpenAlex carries junk duplicate records (wrong DOI/date) for some
    # papers; when the hit is on arXiv, re-fetch canonical metadata so
    # the library record and dedup keys are authoritative.
    if hit.arxiv_id:
        try:
            return arxiv.get_paper(hit.arxiv_id)
        except (ValueError, httpx.HTTPError):
            pass
    return hit


def resolve(ref: str) -> Paper:
    """Resolve an arXiv id/URL, DOI, or title to a Paper."""
    found = doi.extract_doi(ref)
    if found:
        arxiv_id = doi.arxiv_id_from_doi(found)
        if arxiv_id:
            return arxiv.get_paper(arxiv_id)
        return doi.get_paper(found)
    try:
        return arxiv.get_paper(ref)
    except ValueError:
        pass
    paper = _resolve_title(ref)
    if paper:
        return paper
    raise ValueError(
        f"Could not resolve {ref!r} as an arXiv id, DOI, or "
        "confidently-matching title"
    )


def _candidate_pdf_urls(paper: Paper) -> list[str]:
    """PDF URLs to try, most reliable last-resort being arXiv itself."""
    urls = [paper.pdf_url] if paper.pdf_url else []
    if paper.arxiv_id:
        fallback = f"https://arxiv.org/pdf/{paper.arxiv_id}"
        if fallback not in urls:
            urls.append(fallback)
    return urls


def download_pdf(paper: Paper) -> bytes:
    """Download the paper's PDF, falling back to arXiv on dead links.

    Raises ValueError with the underlying cause when no candidate URL
    works.
    """
    urls = _candidate_pdf_urls(paper)
    if not urls:
        raise ValueError(f"No open-access PDF available for: {paper.title}")
    last_error: Exception | None = None
    for url in urls:
        try:
            response = client.get(url)
            response.raise_for_status()
            return response.content
        except httpx.HTTPError as exc:
            last_error = exc
    raise ValueError(
        f"Could not download PDF for {paper.title!r}: {last_error}"
    )


def probe_pdf_size(paper: Paper) -> int | None:
    """Best-effort PDF size in bytes via a HEAD request.

    Returns None when the server does not say.
    """
    for url in _candidate_pdf_urls(paper):
        try:
            response = client.head(url)
            length = response.headers.get("content-length")
            if response.status_code == 200 and length:
                return int(length)
        except httpx.HTTPError:
            continue
    return None
