"""Resolve books to catalog records, independent of delivery.

Books carry ISBNs and book DOIs rather than arXiv ids, and are cataloged
in Zotero as ``book`` items — never queued for e-reader delivery (an
open-access textbook PDF is ingested through ``attach_pdf`` instead).
Resolution tries the most authoritative source for each identifier:
book DOIs via Crossref, ISBNs and titles via Open Library then Google
Books.
"""

import difflib
import re
from dataclasses import dataclass

import httpx

from . import doi as doi_module
from .models import clean_title, normalize_title
from .net import client

_CROSSREF = "https://api.crossref.org/works/"
_OPENLIBRARY_DATA = "https://openlibrary.org/api/books"
_OPENLIBRARY_SEARCH = "https://openlibrary.org/search.json"
_GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"

_TITLE_MATCH_THRESHOLD = 0.8
_ISBN_CHARS = re.compile(r"[^0-9Xx]")
# "ISBN-13: 978-..." is the exact labeled format on retail/publisher
# pages. The digits in "-13"/"-10" would survive character cleaning and
# corrupt the number, so the label is stripped as a unit first. The
# lookahead keeps the 10/13 only when a separator follows — "ISBN 10:"
# is a label, but in "ISBN-1306406152" the 13 belongs to the number.
_ISBN_LABEL = re.compile(
    r"^\s*ISBN(?:[-\s]?1[03](?=[\s:]))?\s*:?\s*", re.IGNORECASE
)


def _clean_isbn(raw: str) -> str:
    return _ISBN_CHARS.sub("", _ISBN_LABEL.sub("", raw)).upper()


def normalize_isbn(raw: str) -> str | None:
    """Return the ISBN-13 form of a valid ISBN-10/13, else None.

    Strips an "ISBN-13:"-style label, hyphens, and spaces, validates
    the check digit, and converts ISBN-10 to ISBN-13 so the same
    edition deduplicates regardless of which form a source reports.
    """
    cleaned = _clean_isbn(raw)
    if len(cleaned) == 10 and _valid_isbn10(cleaned):
        return _isbn10_to_13(cleaned)
    if len(cleaned) == 13 and cleaned.isdigit() and _valid_isbn13(cleaned):
        return cleaned
    return None


def _valid_isbn10(isbn: str) -> bool:
    if not re.fullmatch(r"\d{9}[\dX]", isbn):
        return False
    total = sum(
        (10 - i) * (10 if ch == "X" else int(ch)) for i, ch in enumerate(isbn)
    )
    return total % 11 == 0


def _valid_isbn13(isbn: str) -> bool:
    total = sum((1 if i % 2 == 0 else 3) * int(ch) for i, ch in enumerate(isbn))
    return total % 10 == 0


def _isbn10_to_13(isbn10: str) -> str:
    core = "978" + isbn10[:9]
    check = (
        10
        - sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(core))
        % 10
    ) % 10
    return core + str(check)


@dataclass
class Book:
    """A book catalog record, deduplicated by ISBN.

    ``pdf_url`` is always None here — books are cataloged, not delivered;
    an open-access textbook PDF is ingested through ``attach_pdf``. It
    exists only so ``book`` items share the Zotero-templating helpers
    with papers.
    """

    title: str
    authors: list[str]
    year: str = ""
    publisher: str = ""
    edition: str = ""
    series: str = ""
    isbn: str | None = None
    doi: str | None = None
    num_pages: str = ""
    place: str = ""
    abstract: str = ""
    url: str = ""
    pdf_url: None = None

    def __post_init__(self) -> None:
        self.title = clean_title(self.title)
        if self.isbn:
            self.isbn = normalize_isbn(self.isbn) or self.isbn


def resolve_book(identifier: str) -> Book:
    """Resolve an ISBN, a book DOI, or a title to a Book.

    Raises ValueError when nothing confidently matches; the message
    names the closest candidate so the caller can confirm with a more
    specific identifier.
    """
    ref = identifier.strip()
    if not ref:
        raise ValueError("add_book needs an ISBN, book DOI, or title.")

    isbn13 = normalize_isbn(ref)
    if isbn13:
        book = _from_isbn(isbn13)
        if book:
            return book
        raise ValueError(
            f"No book found for ISBN {ref} in Open Library or Google "
            "Books — check the ISBN, or pass the exact title and author."
        )
    if _looks_like_isbn(ref):
        # Right length and shape for an ISBN, but the check digit fails —
        # a typo, not a title. Say so rather than fruitlessly searching
        # for the digit string as a title.
        raise ValueError(
            f"{ref!r} looks like an ISBN but its check digit is invalid "
            "— re-check the digits."
        )

    found_doi = doi_module.extract_doi(ref)
    if found_doi:
        book = _from_crossref(found_doi)
        if book:
            return book
        raise ValueError(
            f"DOI {found_doi} is not a book Crossref recognizes — for a "
            "journal article use queue_papers/send_papers instead."
        )

    return _from_title(ref)


def _looks_like_isbn(raw: str) -> bool:
    """Whether a string is ISBN-shaped (right length), checksum aside.

    Shares normalize_isbn's cleaner so the two can't disagree on what
    counts as ISBN-shaped, and accepts an X anywhere a check digit
    could sit — a mistyped "...X" 13-form must get the invalid-ISBN
    message, not a doomed title search of the digit string.
    """
    cleaned = _clean_isbn(raw)
    return bool(
        re.fullmatch(r"\d{9}[\dX]", cleaned)
        or re.fullmatch(r"\d{12}[\dX]", cleaned)
    )


def _from_isbn(isbn13: str) -> Book | None:
    # Isolate each backend: Google Books throttles readily, and its 429
    # must not defeat an Open Library hit (or vice versa). Only surface a
    # transient error when NO backend produced a record.
    backend_error: httpx.HTTPError | None = None
    for lookup in (_openlibrary_by_isbn, _google_by_isbn):
        try:
            book = lookup(isbn13)
        except httpx.HTTPError as exc:
            backend_error = backend_error or exc
            continue
        if book:
            return book
    if backend_error is not None:
        raise backend_error
    return None


def _openlibrary_by_isbn(isbn13: str) -> Book | None:
    response = client.get(
        _OPENLIBRARY_DATA,
        params={
            "bibkeys": f"ISBN:{isbn13}",
            "format": "json",
            "jscmd": "data",
        },
    )
    response.raise_for_status()
    record = response.json().get(f"ISBN:{isbn13}")
    if not record:
        return None
    return Book(
        title=record.get("title") or isbn13,
        authors=[a["name"] for a in record.get("authors", []) if a.get("name")],
        year=_year(record.get("publish_date", "")),
        publisher="; ".join(
            p["name"] for p in record.get("publishers", []) if p.get("name")
        ),
        series="; ".join(
            s["name"] for s in record.get("series", []) if isinstance(s, dict)
        ),
        isbn=isbn13,
        num_pages=str(record.get("number_of_pages") or ""),
        place="; ".join(
            p["name"] for p in record.get("publish_places", []) if p.get("name")
        ),
        url=record.get("url", ""),
    )


def _google_by_isbn(isbn13: str) -> Book | None:
    volume = _google_first_volume({"q": f"isbn:{isbn13}"})
    return _book_from_google(volume, isbn13) if volume else None


def _from_crossref(doi: str) -> Book | None:
    response = client.get(_CROSSREF + doi)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    message = response.json()["message"]
    if message.get("type") not in ("book", "monograph", "reference-book"):
        return None
    titles = message.get("title") or []
    isbns = message.get("ISBN") or []
    # Crossref's unknown-date forms include [[null]] AND [[]] (an empty
    # inner list) — indexing [0][0] blindly would crash on the latter.
    date_parts = (message.get("issued", {}).get("date-parts") or [[]])[0]
    year = str(date_parts[0]) if date_parts and date_parts[0] else ""
    return Book(
        title=" ".join(titles[0].split()) if titles else doi,
        authors=_crossref_authors(message),
        year=year,
        publisher=message.get("publisher", ""),
        edition=str(message.get("edition-number") or ""),
        isbn=next((n for n in (normalize_isbn(i) for i in isbns) if n), None),
        doi=doi,
        url=message.get("URL", f"https://doi.org/{doi}"),
    )


def _crossref_authors(message: dict) -> list[str]:
    authors = []
    for author in message.get("author", []):
        parts = (author.get("given"), author.get("family"))
        name = " ".join(p for p in parts if p) or author.get("name", "")
        if name:
            authors.append(name)
    return authors


def _best_book_match(
    ref: str, candidates: list[Book], best: Book | None, best_ratio: float
) -> tuple[Book | None, float]:
    for book in candidates:
        ratio = difflib.SequenceMatcher(
            None, normalize_title(ref), normalize_title(book.title)
        ).ratio()
        if ratio > best_ratio:
            best, best_ratio = book, ratio
    return best, best_ratio


def _from_title(ref: str) -> Book:
    # Isolate each backend (see _from_isbn): a Google Books 429 must not
    # discard a confident Open Library match. Score per backend and stop
    # once one clears the confidence bar — every further call (usually
    # to throttle-prone Google Books) would be wasted work that feeds
    # the very 429s the isolation defends against.
    best: Book | None = None
    best_ratio = 0.0
    backend_error: httpx.HTTPError | None = None
    for lookup in (_openlibrary_by_title, _google_by_title):
        try:
            best, best_ratio = _best_book_match(
                ref, lookup(ref), best, best_ratio
            )
        except httpx.HTTPError as exc:
            backend_error = backend_error or exc
            continue
        if best_ratio >= _TITLE_MATCH_THRESHOLD:
            break
    if best and best_ratio >= _TITLE_MATCH_THRESHOLD:
        return best
    # A confident match would have returned above. With only a weak
    # candidate and a throttled backend, prefer "retry" over offering a
    # possibly-inferior match the failed backend might have beaten.
    if backend_error is not None:
        raise backend_error
    if best:
        who = f" by {best.authors[0]}" if best.authors else ""
        # Only tell the caller to re-run with an ISBN when we actually
        # have one; otherwise pointing them at "the ISBN" is a dead end
        # (and re-running the same title just re-hits this candidate).
        if best.isbn:
            nudge = (
                f"(ISBN {best.isbn}) — confirm with the user, then re-run "
                "add_book with that ISBN."
            )
        else:
            nudge = (
                "but I could not find its ISBN — confirm with the user, "
                "then re-run with a more exact title and author (or the "
                "ISBN if you have it)."
            )
        raise ValueError(
            f"No confident book match for {ref!r}. Closest was "
            f"{best.title!r}{who} {nudge}"
        )
    raise ValueError(
        f"No book found for {ref!r} in Open Library or Google Books — "
        "check the title, or pass the ISBN."
    )


def _openlibrary_by_title(title: str) -> list[Book]:
    response = client.get(
        _OPENLIBRARY_SEARCH, params={"title": title, "limit": 5}
    )
    response.raise_for_status()
    books = []
    for doc in response.json().get("docs", []):
        isbns = doc.get("isbn") or []
        books.append(
            Book(
                title=doc.get("title") or "",
                authors=doc.get("author_name") or [],
                year=str(doc.get("first_publish_year") or ""),
                publisher="; ".join(doc.get("publisher", [])[:1]),
                isbn=next(
                    (n for n in (normalize_isbn(i) for i in isbns) if n), None
                ),
                num_pages=str(doc.get("number_of_pages_median") or ""),
            )
        )
    return books


def _google_by_title(title: str) -> list[Book]:
    response = client.get(
        _GOOGLE_BOOKS, params={"q": f"intitle:{title}", "maxResults": 5}
    )
    response.raise_for_status()
    return [
        book
        for item in response.json().get("items", [])
        if (book := _book_from_google(item, None))
    ]


def _google_first_volume(params: dict) -> dict | None:
    response = client.get(_GOOGLE_BOOKS, params=params)
    response.raise_for_status()
    items = response.json().get("items") or []
    return items[0] if items else None


def _book_from_google(item: dict, isbn13: str | None) -> Book | None:
    info = item.get("volumeInfo") or {}
    if not info.get("title"):
        return None
    identifiers = info.get("industryIdentifiers") or []
    isbn = isbn13
    if not isbn:
        for entry in identifiers:
            candidate = normalize_isbn(entry.get("identifier", ""))
            if candidate:
                isbn = candidate
                break
    return Book(
        title=info["title"],
        authors=info.get("authors") or [],
        year=_year(info.get("publishedDate", "")),
        publisher=info.get("publisher", ""),
        isbn=isbn,
        num_pages=str(info.get("pageCount") or ""),
        url=info.get("canonicalVolumeLink", ""),
    )


def _year(date_text: str) -> str:
    """Pull a four-digit year out of a free-form publication date."""
    match = re.search(r"\d{4}", date_text or "")
    return match.group(0) if match else ""
