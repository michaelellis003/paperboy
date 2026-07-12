"""Turn user-supplied references into Papers and fetch their PDFs.

Accepts arXiv ids ('2401.12345', 'arXiv:...', abs/pdf URLs), DOIs
('10.1038/...', 'doi:...', doi.org URLs), and paper titles, routing
each to the right backend. arXiv's own DataCite DOIs are routed to the
arXiv API. Titles resolve through OpenAlex search, accepted only when
the best hit closely matches the requested title — a wrong paper on
the e-reader is worse than a lookup failure.
"""

import difflib
from urllib.parse import urlsplit

import httpx

from . import arxiv, doi, models, openalex, s2
from .models import Paper, normalize_title
from .net import client

# Auto-accept a title match at or above this; offer (never auto-use) a
# plausible one in [_OFFER, _MATCH); reject anything below _OFFER.
_TITLE_MATCH_THRESHOLD = 0.8
_TITLE_OFFER_THRESHOLD = 0.65


class AmbiguousTitleError(ValueError):
    """A title matched a candidate plausibly but not confidently.

    Subclasses ValueError so every existing resolve() caller declines to
    auto-use the candidate — the "a wrong paper on the e-reader is worse
    than a lookup failure" invariant holds unchanged. Only _resolve_all
    inspects ``candidate`` to offer a "did you mean" the user confirms.
    """

    def __init__(self, candidate: Paper) -> None:
        self.candidate = candidate
        ref = candidate.arxiv_id or candidate.doi
        who = candidate.authors[0] if candidate.authors else "unknown author"
        year = (candidate.published or "")[:4]
        year_note = f" ({year})" if year else ""
        super().__init__(
            f"No confident title match. Did you mean {candidate.title!r} "
            f"by {who}{year_note}? Confirm with the user before re-running "
            f"with ref {ref!r} — do not assume it is the right paper."
        )


def _best_title_match(
    ref: str, candidates: list[Paper]
) -> tuple[Paper | None, float]:
    """Closest candidate to ``ref`` and its similarity ratio."""
    best, best_ratio = None, 0.0
    for paper in candidates:
        ratio = difflib.SequenceMatcher(
            None, normalize_title(ref), normalize_title(paper.title)
        ).ratio()
        if ratio > best_ratio:
            best, best_ratio = paper, ratio
    return best, best_ratio


def _resolve_title(ref: str) -> Paper | None:
    # Try each backend in turn, keeping the strongest match across all of
    # them: relevance ranking sometimes puts a derivative work first, and
    # each backend covers records the others rank poorly (arXiv for
    # canonical preprints, Semantic Scholar for textbooks/older papers
    # OpenAlex misses). Stop early once a confident match appears.
    searches = (
        lambda: openalex.search(ref, max_results=5),
        lambda: arxiv.search(ref, max_results=5),
        lambda: s2.search_title(ref, limit=5),
    )
    best: Paper | None = None
    best_ratio = 0.0
    backend_error: httpx.HTTPError | None = None
    for run in searches:
        try:
            paper, ratio = _best_title_match(ref, run())
        except httpx.HTTPError as exc:
            backend_error = backend_error or exc
            continue
        if ratio > best_ratio:
            best, best_ratio = paper, ratio
        if best_ratio >= _TITLE_MATCH_THRESHOLD:
            break

    if best is not None and best_ratio >= _TITLE_MATCH_THRESHOLD:
        # OpenAlex carries junk duplicate records (wrong DOI/date) for
        # some papers; when the hit is on arXiv, re-fetch canonical
        # metadata so the library record and dedup keys are authoritative.
        if best.arxiv_id:
            try:
                return arxiv.get_paper(best.arxiv_id)
            except (ValueError, httpx.HTTPError):
                pass
        return best
    # A failed search arm must not read as "this title is wrong", and a
    # throttle must not let a mid-band candidate from another backend
    # masquerade as the answer — transient failure takes precedence.
    if backend_error is not None:
        raise backend_error
    # Plausible but not confident: offer it, but only when it carries a
    # concrete id to re-run with. A title-only candidate would re-trigger
    # the identical mid-band match forever, so it hard-fails instead.
    if (
        best is not None
        and best_ratio >= _TITLE_OFFER_THRESHOLD
        and (best.arxiv_id or best.doi)
    ):
        raise AmbiguousTitleError(best)
    return None


# File-share and code hosts serve HTML, not registry records; a raw PDF
# is a payload, not a lookup key — each needs its own honest remedy.
# All host matching is by domain suffix (host == d or host.endswith(.d))
# — bare substring checks misfire ("notgithub.com" contains "github.com";
# "amazon." caught amazon.science, Amazon's research-paper site).
_SHARE_HOSTS = (
    "github.com",
    "githubusercontent.com",
    "gitlab.com",
    "drive.google.com",
    "docs.google.com",
    "dropbox.com",
)
_BOOK_HOSTS = (
    "goodreads.com",
    "amazon.com",
    "amazon.co.uk",
    "amazon.de",
    "amazon.fr",
    "amazon.it",
    "amazon.es",
    "amazon.ca",
    "amazon.co.jp",
    "amazon.com.au",
    "amazon.com.br",
    "amazon.in",
)


def _host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _classify_url(url: str) -> str:
    """An honest, case-specific reason a URL can't be resolved.

    The resolver looks works up in registries and follows the registry's
    OA link; it never fetches a URL. Telling every failure "try the DOI"
    sends users hunting for better URLs that were never going to help, so
    each shape gets the remedy that actually fits.
    """
    parts = urlsplit(url.lower())
    host = parts.netloc.split(":", 1)[0]
    path = parts.path
    if any(_host_matches(host, share) for share in _SHARE_HOSTS):
        return (
            "this is a file-share or code-host link, not a registry "
            "record — use attach_pdf with the PDF, or give a DOI, arXiv "
            "id, or exact title"
        )
    if path.endswith(".pdf"):
        return (
            "this is a direct PDF link. I resolve works through registries "
            "(arXiv, Crossref, OpenAlex), not by fetching URLs — use "
            "attach_pdf to ingest a PDF you already have"
        )
    # "bookstore." is a leading label (bookstore.ams.org, bookstore.gpo.gov);
    # the path check is on whole segments so /bookmark//booklets don't hit.
    segments = [s for s in path.split("/") if s]
    if (
        any(_host_matches(host, book) for book in _BOOK_HOSTS)
        or host.startswith("bookstore.")
        or "book" in segments
        or "books" in segments
    ):
        return (
            "this looks like a book (retail or catalog page) — use "
            "add_book with the ISBN, or the title and author"
        )
    return (
        "publisher landing URLs are not supported — try the DOI or exact "
        "title, or attach_pdf if you have the PDF"
    )


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
    if ref.strip().lower().startswith(("http://", "https://")):
        # Any URL that survived the DOI and arXiv branches can't be
        # resolved by lookup. Title search can never rescue it (a URL
        # won't fuzzy-match a title), so fail fast with a case-specific
        # remedy — even mid-throttle, when a backend call here would
        # misreport this as a transient failure.
        raise ValueError(f"Could not resolve {ref!r}: {_classify_url(ref)}")
    paper = _resolve_title(ref)
    if paper:
        return paper
    raise ValueError(
        f"Could not resolve {ref!r} as an arXiv id, DOI, or "
        "confidently-matching title"
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
    for fallback in models.biorxiv_pdf_urls(paper.doi):
        if fallback not in urls:
            urls.append(fallback)
    return urls


# Hard cap for attach_pdf downloads. The deployed instance runs with
# 512 MiB and briefly holds two copies (bytes + a tmpfs temp file that
# counts against the same memory limit), so an uncapped download of a
# caller-supplied URL could OOM the whole server. 100 MB covers any
# real textbook PDF with headroom to spare.
_MAX_FETCH_BYTES = 100_000_000


def fetch_pdf(url: str) -> bytes:
    """Download one URL, verifying the payload is really a PDF.

    Used by attach_pdf to ingest a PDF the user points at directly (no
    registry record involved) — the one download path fed arbitrary
    URLs, so it streams with a size cap instead of buffering blindly.
    Raises ValueError when the URL serves non-PDF content (an HTML
    anti-bot or landing page) or exceeds the cap, and re-raises httpx
    errors for transient transport failures.
    """
    too_big = ValueError(
        f"{url} is larger than the {_MAX_FETCH_BYTES // 1_000_000} MB "
        "attach_pdf limit"
    )
    with client.stream("GET", url) as response:
        response.raise_for_status()
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > _MAX_FETCH_BYTES:
            raise too_big
        chunks: list[bytes] = []
        total = 0
        checked = False
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > _MAX_FETCH_BYTES:
                raise too_big
            chunks.append(chunk)
            # Reject non-PDF payloads on the first KB rather than after
            # downloading a whole HTML page (or worse).
            if not checked and total >= 1024:
                checked = True
                if b"%PDF-" not in b"".join(chunks)[:1024]:
                    content_type = response.headers.get(
                        "content-type", "unknown"
                    )
                    raise ValueError(
                        f"{url} returned non-PDF content ({content_type})"
                    )
    content = b"".join(chunks)
    if b"%PDF-" not in content[:1024]:
        content_type = response.headers.get("content-type", "unknown")
        raise ValueError(f"{url} returned non-PDF content ({content_type})")
    return content


def download_pdf(paper: Paper) -> bytes:
    """Download the paper's PDF, falling back to arXiv on dead links.

    Raises ValueError only when failure is deterministic for every
    candidate URL (dead links, non-PDF junk); if ANY candidate failed
    transiently (timeout, 5xx, 429), that httpx error is re-raised so
    callers leave the paper unsent for retry instead of writing it
    off as PDF-less.
    """
    urls = _candidate_pdf_urls(paper)
    if not urls:
        raise ValueError(f"No open-access PDF available for: {paper.title}")
    last_error: Exception | None = None
    transient_error: httpx.HTTPError | None = None
    for url in urls:
        try:
            response = client.get(url)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (408, 429) or status >= 500:
                transient_error = transient_error or exc
            last_error = exc
            continue
        except httpx.HTTPError as exc:
            transient_error = transient_error or exc
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
    if transient_error is not None:
        # ANY candidate failing transiently means retry might succeed:
        # a bioRxiv 503 followed by the medRxiv mirror's guaranteed 404
        # must not read as "no usable PDF" and earn a permanent tag.
        raise transient_error
    # Deterministic: dead links (404) or non-PDF payloads on every
    # candidate URL — retrying won't change the outcome.
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
