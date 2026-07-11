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
    ratio = difflib.SequenceMatcher(
        None, _normalize(ref), _normalize(matches[0].title)
    ).ratio()
    return matches[0] if ratio >= _TITLE_MATCH_THRESHOLD else None


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


def download_pdf(paper: Paper) -> bytes:
    """Download the paper's PDF; raises when no OA PDF is available."""
    if not paper.pdf_url:
        raise ValueError(f"No open-access PDF available for: {paper.title}")
    response = client.get(paper.pdf_url)
    response.raise_for_status()
    return response.content
