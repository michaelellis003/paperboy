"""General paper search via OpenAlex.

OpenAlex indexes ~250M scholarly works across publishers and preprint
servers (arXiv included) and embeds Unpaywall's open-access data, so
search results come back with a direct OA PDF link when one exists. No
API key is needed; a contact email opts into the faster polite pool.
"""

import re
import time

from .config import settings
from .models import Paper
from .net import client

_API = "https://api.openalex.org/works"
_ARXIV_ABS = re.compile(
    r"arxiv\.org/(?:abs|pdf)/([^\s?#]+?)(?:v\d+)?(?:\.pdf)?$"
)


def _abstract_from_inverted_index(index: dict | None) -> str:
    """Rebuild abstract text from OpenAlex's inverted index."""
    if not index:
        return ""
    positions = [
        (position, word)
        for word, places in index.items()
        for position in places
    ]
    return " ".join(word for _, word in sorted(positions))


def _date_from_arxiv_id(arxiv_id: str) -> str | None:
    """Derive YYYY-MM from an arXiv id.

    OpenAlex often reports the latest version's date (e.g. 2025 for a
    2017 paper), which misleads recency-based picks; the id itself
    encodes the true submission month.
    """
    match = re.match(r"^(\d{2})(\d{2})\.\d{4,5}$", arxiv_id)
    if not match:
        match = re.match(r"^[a-z-]+/(\d{2})(\d{2})\d{3}$", arxiv_id)
    if not match:
        return None
    yy, mm = match.groups()
    century = "19" if int(yy) >= 91 else "20"
    return f"{century}{yy}-{mm}"


def _arxiv_id(work: dict) -> str | None:
    locations = [
        work.get("primary_location"),
        work.get("best_oa_location"),
        *(work.get("locations") or []),
    ]
    for location in locations:
        if not location:
            continue
        for key in ("landing_page_url", "pdf_url"):
            match = _ARXIV_ABS.search(location.get(key) or "")
            if match:
                return match.group(1)
    return None


def _parse_work(work: dict) -> Paper:
    doi_url = work.get("doi") or ""
    doi = doi_url.removeprefix("https://doi.org/") or None
    best_oa = work.get("best_oa_location") or {}
    primary = work.get("primary_location") or {}
    url = doi_url or primary.get("landing_page_url") or work.get("id", "")
    arxiv_id = _arxiv_id(work)
    # arXiv's own PDF endpoint is far more reliable than whatever OA
    # mirror OpenAlex ranked "best" — prefer it whenever the paper is
    # on arXiv.
    pdf_url = (
        f"https://arxiv.org/pdf/{arxiv_id}"
        if arxiv_id
        else best_oa.get("pdf_url")
    )
    return Paper(
        title=work.get("display_name") or "(untitled)",
        authors=[
            authorship["author"]["display_name"]
            for authorship in work.get("authorships", [])
            if authorship.get("author", {}).get("display_name")
        ],
        abstract=_abstract_from_inverted_index(
            work.get("abstract_inverted_index")
        ),
        published=(
            (arxiv_id and _date_from_arxiv_id(arxiv_id))
            or work.get("publication_date")
            or ""
        ),
        url=url,
        pdf_url=pdf_url,
        arxiv_id=arxiv_id,
        doi=doi,
    )


def search(query: str, max_results: int = 5) -> list[Paper]:
    """Search OpenAlex by relevance and return up to ``max_results``."""
    # OpenAlex rejects wildcard characters in search strings with HTTP
    # 400 — and titles ending in '?' are common. Spaces are equivalent
    # for relevance search.
    cleaned = query.replace("*", " ").replace("?", " ").strip()
    params: dict[str, str | int] = {
        "search": cleaned,
        "per-page": max_results,
    }
    email = settings().polite_email
    if email:
        params["mailto"] = email
    response = client.get(_API, params=params)
    if response.status_code == 429:
        # One brief retry: OpenAlex throttles bursts, and a first-time
        # user whose very first query dies to a 429 concludes the tool
        # is broken.
        time.sleep(1.5)
        response = client.get(_API, params=params)
    response.raise_for_status()
    return [_parse_work(work) for work in response.json().get("results", [])]
