import httpx
import pytest

from paperboy import books

# Real, valid ISBNs used across the suite.
LEBESGUE_13 = "9781470421991"  # Nelson, STML-78 (as ISBN-13)
WP_ISBN10 = "0-306-40615-2"  # classic ISBN-10 example
WP_ISBN13 = "9780306406157"  # its ISBN-13 form


def use_client(monkeypatch, handler):
    monkeypatch.setattr(
        books, "client", httpx.Client(transport=httpx.MockTransport(handler))
    )


# --- ISBN normalization -------------------------------------------------


def test_normalize_isbn13_strips_hyphens():
    assert books.normalize_isbn("978-1-4704-2199-1") == LEBESGUE_13


def test_normalize_isbn10_converts_to_13():
    assert books.normalize_isbn(WP_ISBN10) == WP_ISBN13


def test_isbn10_and_13_of_same_edition_normalize_equal():
    assert books.normalize_isbn(WP_ISBN10) == books.normalize_isbn(WP_ISBN13)


def test_normalize_isbn_rejects_bad_check_digit():
    assert books.normalize_isbn("978-1-4704-2199-9") is None


def test_normalize_isbn_rejects_non_isbn():
    assert books.normalize_isbn("not-an-isbn") is None


# --- ISBN resolution ----------------------------------------------------


def test_isbn_resolves_via_openlibrary(monkeypatch):
    def handler(request):
        if "openlibrary.org/api/books" in str(request.url):
            return httpx.Response(
                200,
                json={
                    f"ISBN:{LEBESGUE_13}": {
                        "title": "A User-Friendly Introduction to Lebesgue "
                        "Measure and Integration",
                        "authors": [{"name": "Gail S. Nelson"}],
                        "publishers": [
                            {"name": "American Mathematical Society"}
                        ],
                        "publish_date": "2015",
                        "number_of_pages": 221,
                    }
                },
            )
        raise AssertionError(f"unexpected call: {request.url}")

    use_client(monkeypatch, handler)
    book = books.resolve_book("978-1-4704-2199-1")
    assert book.isbn == LEBESGUE_13
    assert book.authors == ["Gail S. Nelson"]
    assert book.publisher == "American Mathematical Society"
    assert book.num_pages == "221"


def test_isbn_falls_back_to_google_books(monkeypatch):
    def handler(request):
        if "openlibrary.org" in str(request.url):
            return httpx.Response(200, json={})  # OL has no record
        if "googleapis.com/books" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "volumeInfo": {
                                "title": "Graphical Models",
                                "authors": ["Steffen L. Lauritzen"],
                                "publisher": "Oxford University Press",
                                "publishedDate": "1996-05-09",
                                "pageCount": 312,
                                "industryIdentifiers": [
                                    {
                                        "type": "ISBN_13",
                                        "identifier": LEBESGUE_13,
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected call: {request.url}")

    use_client(monkeypatch, handler)
    book = books.resolve_book(LEBESGUE_13)
    assert book.title == "Graphical Models"
    assert book.year == "1996"
    assert book.isbn == LEBESGUE_13


def test_isbn_not_found_anywhere_raises(monkeypatch):
    use_client(monkeypatch, lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="No book found for ISBN"):
        books.resolve_book(LEBESGUE_13)


# --- book DOI resolution ------------------------------------------------


def test_book_doi_resolves_via_crossref(monkeypatch):
    def handler(request):
        assert "api.crossref.org/works/" in str(request.url)
        return httpx.Response(
            200,
            json={
                "message": {
                    "type": "book",
                    "title": ["Measure Theory"],
                    "author": [{"given": "Edward", "family": "Nelson"}],
                    "publisher": "AMS",
                    "issued": {"date-parts": [[2015]]},
                    "ISBN": ["978-1-4704-2199-1"],
                    "edition-number": "1",
                }
            },
        )

    use_client(monkeypatch, handler)
    book = books.resolve_book("10.1090/stml/078")
    assert book.doi == "10.1090/stml/078"
    assert book.publisher == "AMS"
    assert book.isbn == LEBESGUE_13
    assert book.edition == "1"


def test_journal_doi_is_not_a_book(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, json={"message": {"type": "journal-article", "title": ["X"]}}
        )

    use_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="not a book"):
        books.resolve_book("10.1038/nature12373")


# --- title resolution ---------------------------------------------------


def _title_handler(docs, google_items=None):
    def handler(request):
        if "openlibrary.org/search.json" in str(request.url):
            return httpx.Response(200, json={"docs": docs})
        if "googleapis.com/books" in str(request.url):
            return httpx.Response(200, json={"items": google_items or []})
        raise AssertionError(f"unexpected call: {request.url}")

    return handler


def test_title_confident_match_resolves(monkeypatch):
    docs = [
        {
            "title": "Advanced Data Analysis from an Elementary Point of View",
            "author_name": ["Cosma Shalizi"],
            "first_publish_year": 2023,
            "isbn": ["9781470421991"],
        }
    ]
    use_client(monkeypatch, _title_handler(docs))
    book = books.resolve_book(
        "Advanced Data Analysis from an Elementary Point of View"
    )
    assert book.authors == ["Cosma Shalizi"]
    assert book.isbn == LEBESGUE_13


def test_title_weak_match_offers_candidate(monkeypatch):
    docs = [
        {
            "title": "Data Analysis Using Regression",
            "author_name": ["Somebody Else"],
            "isbn": ["9780306406157"],
        }
    ]
    use_client(monkeypatch, _title_handler(docs))
    with pytest.raises(ValueError, match="Closest was"):
        books.resolve_book(
            "Advanced Data Analysis from an Elementary Point of View"
        )


def test_title_no_results_raises(monkeypatch):
    use_client(monkeypatch, _title_handler([]))
    with pytest.raises(ValueError, match="No book found"):
        books.resolve_book("A Title That Does Not Exist Anywhere At All")


def test_empty_identifier_raises(monkeypatch):
    use_client(monkeypatch, lambda r: httpx.Response(200, json={}))
    with pytest.raises(ValueError, match="needs an ISBN"):
        books.resolve_book("   ")


# --- per-backend HTTP-error isolation (round-1 certification) -----------


def _throttle(request):
    return httpx.Response(429)


def test_isbn_confident_openlibrary_survives_google_throttle(monkeypatch):
    def handler(request):
        if "openlibrary.org/api/books" in str(request.url):
            return httpx.Response(
                200,
                json={
                    f"ISBN:{LEBESGUE_13}": {
                        "title": "Lebesgue Measure",
                        "authors": [{"name": "Nelson"}],
                    }
                },
            )
        return httpx.Response(429)  # Google Books throttles

    use_client(monkeypatch, handler)
    book = books.resolve_book(LEBESGUE_13)
    assert book.title == "Lebesgue Measure"


def test_isbn_falls_through_google_when_openlibrary_throttles(monkeypatch):
    def handler(request):
        if "openlibrary.org" in str(request.url):
            return httpx.Response(429)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "volumeInfo": {
                            "title": "Recovered via Google",
                            "authors": ["A"],
                        }
                    }
                ]
            },
        )

    use_client(monkeypatch, handler)
    assert books.resolve_book(LEBESGUE_13).title == "Recovered via Google"


def test_isbn_all_backends_throttle_raises_transient(monkeypatch):
    use_client(monkeypatch, _throttle)
    with pytest.raises(httpx.HTTPStatusError):
        books.resolve_book(LEBESGUE_13)


def test_title_confident_openlibrary_survives_google_throttle(monkeypatch):
    def handler(request):
        if "openlibrary.org/search.json" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "title": "Graphical Models",
                            "author_name": ["Lauritzen"],
                        }
                    ]
                },
            )
        return httpx.Response(429)  # Google Books throttles

    use_client(monkeypatch, handler)
    assert books.resolve_book("Graphical Models").title == "Graphical Models"


def test_title_weak_match_with_throttle_prefers_retry(monkeypatch):
    # Weak OL candidate + Google 429: surface the transient error, not the
    # "closest was" offer (Google might have had the confident match).
    def handler(request):
        if "openlibrary.org/search.json" in str(request.url):
            return httpx.Response(
                200, json={"docs": [{"title": "Totally Different Book"}]}
            )
        return httpx.Response(429)

    use_client(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        books.resolve_book("Advanced Data Analysis Elementary Point of View")


# --- invalid ISBN gets a targeted message ------------------------------


def test_invalid_isbn_check_digit_is_flagged_as_isbn(monkeypatch):
    # No network should be touched: a bad-check-digit ISBN is reported as
    # such, not searched as a title.
    def handler(request):
        raise AssertionError("must not hit the network for a bad ISBN")

    use_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="looks like an ISBN"):
        books.resolve_book("978-1-4704-2199-9")


def test_title_offer_with_isbn_points_at_that_isbn(monkeypatch):
    docs = [
        {"title": "Data Analysis Using Regression", "isbn": ["9780306406157"]}
    ]
    use_client(monkeypatch, _title_handler(docs))
    with pytest.raises(ValueError, match="re-run add_book with that ISBN"):
        books.resolve_book("Advanced Data Analysis Elementary Point of View")


def test_title_offer_without_isbn_does_not_point_at_an_isbn(monkeypatch):
    # Closest candidate has NO ISBN: the message must not tell the caller
    # to "re-run with the ISBN" it just said it couldn't find.
    docs = [{"title": "Data Analysis Using Regression", "author_name": ["K"]}]
    use_client(monkeypatch, _title_handler(docs))
    with pytest.raises(ValueError) as info:
        books.resolve_book("Advanced Data Analysis Elementary Point of View")
    message = str(info.value)
    assert "could not find its ISBN" in message
    assert "re-run add_book with that ISBN" not in message


# --- review-round fixes -------------------------------------------------


def test_isbn13_labeled_prefix_normalizes():
    # "ISBN-13: 978-..." is the labeled format on retail/publisher pages;
    # the digits in the label must not corrupt the number.
    assert books.normalize_isbn("ISBN-13: 978-0-306-40615-7") == WP_ISBN13
    assert books.normalize_isbn("ISBN-10: 0-306-40615-2") == WP_ISBN13
    assert books.normalize_isbn("isbn 978-0-306-40615-7") == WP_ISBN13


def test_isbn13_with_x_typo_gets_invalid_isbn_message(monkeypatch):
    # An ISBN-13 shape with an X (invalid) must get the ISBN message,
    # not a doomed title search of the digit string.
    def handler(request):
        raise AssertionError("must not hit the network for a bad ISBN")

    use_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="looks like an ISBN"):
        books.resolve_book("978030640615X")


def test_crossref_empty_inner_date_parts_does_not_crash(monkeypatch):
    # Crossref emits date-parts: [[]] for unknown dates; indexing [0][0]
    # blindly crashed. The book must resolve with an empty year.
    def handler(request):
        return httpx.Response(
            200,
            json={
                "message": {
                    "type": "book",
                    "title": ["Undated Book"],
                    "issued": {"date-parts": [[]]},
                }
            },
        )

    use_client(monkeypatch, handler)
    book = books.resolve_book("10.1090/stml/078")
    assert book.title == "Undated Book"
    assert book.year == ""


def test_title_arm_skips_google_after_confident_openlibrary_match(
    monkeypatch,
):
    # Google Books throttles readily; once Open Library has a confident
    # match the Google call is wasted work and must not happen.
    def handler(request):
        if "openlibrary.org/search.json" in str(request.url):
            return httpx.Response(
                200,
                json={"docs": [{"title": "Graphical Models"}]},
            )
        raise AssertionError("Google Books must not be called")

    use_client(monkeypatch, handler)
    assert books.resolve_book("Graphical Models").title == "Graphical Models"


def test_isbn_label_with_space_before_digits_normalizes():
    assert books.normalize_isbn("ISBN 10: 0-306-40615-2") == WP_ISBN13
    assert books.normalize_isbn("ISBN 13 978-0-306-40615-7") == WP_ISBN13


def test_isbn_label_regex_does_not_eat_number_digits():
    # "13"/"10" directly attached to the number belong to the NUMBER;
    # stripping them would corrupt a legitimate ISBN.
    assert books._clean_isbn("ISBN-1306406152") == "1306406152"
    assert books._clean_isbn("ISBN 1078454220") == "1078454220"
