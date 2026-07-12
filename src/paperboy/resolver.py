"""Turn user-supplied references into Papers and fetch their PDFs.

Accepts arXiv ids ('2401.12345', 'arXiv:...', abs/pdf URLs), DOIs
('10.1038/...', 'doi:...', doi.org URLs), and paper titles, routing
each to the right backend. arXiv's own DataCite DOIs are routed to the
arXiv API. Titles resolve through OpenAlex search, accepted only when
the best hit closely matches the requested title — a wrong paper on
the e-reader is worse than a lookup failure.
"""

import contextlib
import difflib

import httpx

from . import arxiv, doi, openalex
from .models import Paper, normalize_title
from .net import client

_TITLE_MATCH_THRESHOLD = 0.8


def _best_title_match(ref: str, candidates: list[Paper]) -> Paper | None:
    best, best_ratio = None, 0.0
    for paper in candidates:
        ratio = difflib.SequenceMatcher(
            None, normalize_title(ref), normalize_title(paper.title)
        ).ratio()
        if ratio > best_ratio:
            best, best_ratio = paper, ratio
    return best if best_ratio >= _TITLE_MATCH_THRESHOLD else None


def _resolve_title(ref: str) -> Paper | None:
    # Scan several hits: relevance ranking sometimes puts a derivative
    # work first (e.g. Sentence-BERT above BERT for the exact BERT
    # title), and arXiv covers canonical records OpenAlex ranks poorly.
    hit = None
    with contextlib.suppress(httpx.HTTPError):
        hit = _best_title_match(ref, openalex.search(ref, max_results=5))
    if hit is None:
        with contextlib.suppress(httpx.HTTPError):
            hit = _best_title_match(ref, arxiv.search(ref, max_results=5))
    if hit is None:
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
    hint = (
        " (publisher landing URLs are not supported — try the DOI or "
        "exact title)"
        if ref.startswith(("http://", "https://"))
        else ""
    )
    raise ValueError(
        f"Could not resolve {ref!r} as an arXiv id, DOI, or "
        f"confidently-matching title{hint}"
    )


def _candidate_pdf_urls(paper: Paper) -> list[str]:
    """PDF URLs to try, with source-native fallbacks appended.

    The OA index sometimes has no PDF link for a preprint that is in
    fact freely available at the source (bioRxiv/medRxiv preprints are
    open by definition, but their DOIs occasionally lack an OA link
    upstream). Appending the source's own PDF URL recovers those; the
    download path verifies the payload is a real PDF, so a wrong guess
    fails safely rather than shipping an HTML page.
    """
    urls = [paper.pdf_url] if paper.pdf_url else []
    if paper.arxiv_id:
        fallback = f"https://arxiv.org/pdf/{paper.arxiv_id}"
        if fallback not in urls:
            urls.append(fallback)
    if paper.doi and paper.doi.startswith("10.1101/"):
        # bioRxiv and medRxiv share the 10.1101 prefix and serve the PDF
        # at a DOI-derived path that redirects to the latest version.
        # We can't tell the two apart from the DOI, so offer both hosts;
        # the wrong one 404s and is skipped by the PDF-verifying download.
        for host in ("www.biorxiv.org", "www.medrxiv.org"):
            fallback = f"https://{host}/content/{paper.doi}.full.pdf"
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
        except httpx.HTTPError as exc:
            last_error = exc
            continue
        # OA links sometimes return HTTP 200 with an HTML anti-bot or
        # landing page — shipping that to an e-reader as a "PDF" is
        # worse than failing, so verify the payload really is one.
        if b"%PDF-" not in response.content[:1024]:
            content_type = response.headers.get("content-type", "unknown")
            last_error = ValueError(
                f"{url} returned non-PDF content ({content_type})"
            )
            continue
        return response.content
    raise ValueError(
        f"Could not download PDF for {paper.title!r}: {last_error}"
    )


# A real paper PDF is well over this; a HEAD reporting less than this
# is measuring a redirect or landing stub, not the document, so we treat
# it as unknown rather than reporting a misleading "0.0 MB".
_MIN_PLAUSIBLE_PDF_BYTES = 50_000


def probe_pdf_size(paper: Paper) -> int | None:
    """Best-effort PDF size in bytes via a HEAD request.

    Returns None when the server does not report a plausible size.
    """
    for url in _candidate_pdf_urls(paper):
        try:
            response = client.head(url)
            length = response.headers.get("content-length")
            if response.status_code == 200 and length:
                size = int(length)
                if size >= _MIN_PLAUSIBLE_PDF_BYTES:
                    return size
        except httpx.HTTPError:
            continue
    return None
