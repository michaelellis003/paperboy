"""Paper recommendations via the Semantic Scholar Recommendations API.

Given seed papers (by arXiv id or DOI), returns citation-graph
neighbors — papers that people who cared about the seeds also care
about. The ``recent`` pool favors newly published work; ``all-cs`` is
the all-time pool but covers computer science only (an upstream
limitation of the API).
"""

from .models import Paper
from .net import client

_API = "https://api.semanticscholar.org/recommendations/v1/papers/"
_FIELDS = (
    "title,abstract,year,publicationDate,authors,externalIds,openAccessPdf"
)


def _parse(rec: dict) -> Paper:
    ext = rec.get("externalIds") or {}
    arxiv_id = ext.get("ArXiv")
    doi = ext.get("DOI")
    oa_pdf = (rec.get("openAccessPdf") or {}).get("url") or None
    if arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"
    elif doi:
        url = f"https://doi.org/{doi}"
        pdf_url = oa_pdf
    else:
        url = f"https://www.semanticscholar.org/paper/{rec.get('paperId', '')}"
        pdf_url = oa_pdf
    return Paper(
        title=rec.get("title") or "(untitled)",
        authors=[
            author["name"]
            for author in rec.get("authors") or []
            if author.get("name")
        ],
        abstract=rec.get("abstract") or "",
        published=rec.get("publicationDate") or str(rec.get("year") or ""),
        url=url,
        pdf_url=pdf_url,
        arxiv_id=arxiv_id,
        doi=doi,
    )


def recommend(
    seed_ids: list[str], pool: str = "recent", limit: int = 10
) -> list[Paper]:
    """Citation-graph recommendations for S2-style seed ids.

    ``seed_ids`` use the API's prefixes: 'ArXiv:2312.00752' or
    'DOI:10.1038/...'. ``pool`` is 'recent' or 'all-cs'.
    """
    response = client.post(
        _API,
        params={"fields": _FIELDS, "limit": limit, "from": pool},
        json={"positivePaperIds": seed_ids},
    )
    response.raise_for_status()
    return [_parse(rec) for rec in response.json().get("recommendedPapers", [])]
