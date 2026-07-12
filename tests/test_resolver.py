import httpx
import pytest

from paperboy import arxiv, doi, openalex, resolver, s2


@pytest.fixture(autouse=True)
def _no_network_s2(monkeypatch):
    """Default the Semantic Scholar title arm to empty.

    Tests that exercise the S2 fallback override this explicitly; the
    rest must never make a real network call when a title reaches it.
    """
    monkeypatch.setattr(s2, "search_title", lambda q, limit=5: [])


def test_doi_refs_go_to_crossref(monkeypatch, paper_factory):
    sentinel = paper_factory(doi="10.1038/nature12373", arxiv_id=None)
    monkeypatch.setattr(doi, "get_paper", lambda d: sentinel)
    assert resolver.resolve("https://doi.org/10.1038/nature12373") is sentinel


def test_arxiv_datacite_doi_goes_to_arxiv(monkeypatch, paper_factory):
    sentinel = paper_factory()
    seen = {}
    monkeypatch.setattr(
        arxiv, "get_paper", lambda ref: seen.setdefault("ref", ref) and sentinel
    )
    resolver.resolve("10.48550/arXiv.2005.14165")
    assert seen["ref"] == "2005.14165"


def test_arxiv_refs_go_to_arxiv(monkeypatch, paper_factory):
    sentinel = paper_factory()
    monkeypatch.setattr(arxiv, "get_paper", lambda ref: sentinel)
    assert resolver.resolve("arXiv:2401.12345") is sentinel


def test_title_resolves_via_openalex(monkeypatch, paper_factory):
    hit = paper_factory(
        title="Attention Is All You Need", arxiv_id=None, doi="10.1/x"
    )
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [hit])
    assert resolver.resolve("attention is all you need") is hit


def test_title_hit_on_arxiv_is_canonicalized(monkeypatch, paper_factory):
    junk = paper_factory(
        title="Attention Is All You Need",
        doi="10.65215/junk-duplicate",
        published="2025-08-23",
    )
    canonical = paper_factory(title="Attention Is All You Need")
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [junk])
    monkeypatch.setattr(arxiv, "get_paper", lambda ref: canonical)
    assert resolver.resolve("attention is all you need") is canonical


def test_title_with_weak_match_rejected(monkeypatch, paper_factory):
    hit = paper_factory(title="Something Entirely Different")
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [hit])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="confidently-matching title"):
        resolver.resolve("attention is all you need")


def test_title_scans_beyond_top_hit(monkeypatch, paper_factory):
    # relevance ranking may put a derivative work first (Sentence-BERT
    # above BERT); the true match deeper in the list must win
    derivative = paper_factory(
        title="Sentence-BERT: Sentence Embeddings using Siamese Networks",
        arxiv_id=None,
        doi="10.1/sbert",
    )
    canonical = paper_factory(
        title="BERT: Pre-training of Deep Bidirectional Transformers "
        "for Language Understanding",
        arxiv_id=None,
        doi="10.1/bert",
    )
    monkeypatch.setattr(
        openalex, "search", lambda q, max_results: [derivative, canonical]
    )
    resolved = resolver.resolve(
        "BERT: Pre-training of Deep Bidirectional Transformers for "
        "Language Understanding"
    )
    assert resolved is canonical


def test_title_falls_back_to_arxiv_search(monkeypatch, paper_factory):
    wanted = paper_factory(title="A Very Specific Preprint Title")
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [wanted])
    monkeypatch.setattr(arxiv, "get_paper", lambda ref: wanted)
    assert resolver.resolve("A Very Specific Preprint Title") is wanted


def test_unresolvable_ref_raises(monkeypatch):
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="arXiv id, DOI, or"):
        resolver.resolve("my cool paper")


def test_title_backend_failure_is_not_could_not_resolve(
    monkeypatch, paper_factory
):
    # An OpenAlex 429 must surface as a transient error, not read as
    # "this title is wrong" — the receipts route HTTP errors to a
    # "temporarily unreachable — retry" message.
    request = httpx.Request("GET", "https://api.openalex.org/works")

    def throttled(q, max_results):
        raise httpx.HTTPStatusError(
            "429",
            request=request,
            response=httpx.Response(429, request=request),
        )

    monkeypatch.setattr(openalex, "search", throttled)
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(httpx.HTTPStatusError):
        resolver.resolve("Denoising Diffusion Probabilistic Models")


def test_title_backend_failure_recovered_by_arxiv_arm(
    monkeypatch, paper_factory
):
    # When arXiv still finds a confident match, an OpenAlex outage is
    # irrelevant and resolution succeeds.
    wanted = paper_factory(title="A Very Specific Preprint Title")

    def down(q, max_results):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(openalex, "search", down)
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [wanted])
    monkeypatch.setattr(arxiv, "get_paper", lambda ref: wanted)
    assert resolver.resolve("A Very Specific Preprint Title") is wanted


def test_download_pdf(monkeypatch, paper_factory):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=b"%PDF-1.4")
            )
        ),
    )
    assert resolver.download_pdf(paper_factory()) == b"%PDF-1.4"


def test_download_pdf_without_oa_pdf_raises(paper_factory):
    with pytest.raises(ValueError, match="No open-access PDF"):
        resolver.download_pdf(paper_factory(pdf_url=None, arxiv_id=None))


def test_download_falls_back_to_arxiv(monkeypatch, paper_factory):
    def handler(request):
        if request.url.host == "arxiv.org":
            return httpx.Response(200, content=b"%PDF-arxiv")
        return httpx.Response(404)

    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    paper = paper_factory(pdf_url="https://deadmirror.invalid/x.pdf")
    assert resolver.download_pdf(paper) == b"%PDF-arxiv"


def test_download_falls_back_to_biorxiv_for_preprint_doi(
    monkeypatch, paper_factory
):
    # A bioRxiv DOI whose OA index has no PDF link: recover it from
    # bioRxiv's own DOI-derived PDF path.
    def handler(request):
        if request.url.host == "www.biorxiv.org":
            return httpx.Response(200, content=b"%PDF-biorxiv")
        return httpx.Response(404)

    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    paper = paper_factory(pdf_url=None, arxiv_id=None, doi="10.1101/820936")
    assert resolver.download_pdf(paper) == b"%PDF-biorxiv"


def test_biorxiv_fallback_only_for_preprint_dois(paper_factory):
    journal = paper_factory(pdf_url=None, arxiv_id=None, doi="10.1038/x")
    assert not resolver._candidate_pdf_urls(journal)
    preprint = paper_factory(pdf_url=None, arxiv_id=None, doi="10.1101/820936")
    urls = resolver._candidate_pdf_urls(preprint)
    assert any("biorxiv.org" in u for u in urls)
    assert any("medrxiv.org" in u for u in urls)


def test_download_rejects_html_masquerading_as_pdf(monkeypatch, paper_factory):
    def handler(request):
        if request.url.host == "arxiv.org":
            return httpx.Response(200, content=b"%PDF-real")
        return httpx.Response(
            200,
            content=b"<html>Incapsula anti-bot page</html>",
            headers={"content-type": "text/html"},
        )

    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    paper = paper_factory(pdf_url="https://blocked.invalid/x.pdf")
    assert resolver.download_pdf(paper) == b"%PDF-real"


def test_download_all_candidates_non_pdf_raises(monkeypatch, paper_factory):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    content=b"<html>nope</html>",
                    headers={"content-type": "text/html"},
                )
            )
        ),
    )
    with pytest.raises(ValueError, match="non-PDF content"):
        resolver.download_pdf(paper_factory())


def test_publisher_url_error_includes_hint(monkeypatch):
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="publisher landing URLs"):
        resolver.resolve("https://www.nature.com/articles/s41586-x")


def test_ieee_style_url_not_parsed_as_arxiv_id(monkeypatch):
    # A path like /document/9999999 matches the old-style-id shape; the
    # resolver must NOT fire a bogus arXiv API call for it, and must
    # surface the publisher-landing hint instead. openalex/arxiv search
    # are stubbed so any real network call would be the bug.
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="publisher landing URLs"):
        resolver.resolve("https://ieeexplore.ieee.org/document/9999999")


def test_download_reports_cause_when_all_fail(monkeypatch, paper_factory):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ),
    )
    with pytest.raises(ValueError, match="Could not download PDF"):
        resolver.download_pdf(paper_factory())


def test_probe_pdf_size(monkeypatch, paper_factory):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, headers={"content-length": "123456"}
                )
            )
        ),
    )
    assert resolver.probe_pdf_size(paper_factory()) == 123456


def test_probe_pdf_size_unknown(monkeypatch, paper_factory):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(lambda r: httpx.Response(404))
        ),
    )
    assert resolver.probe_pdf_size(paper_factory()) is None


def test_probe_pdf_size_ignores_implausibly_small(monkeypatch, paper_factory):
    # A HEAD reporting a few KB is a redirect/landing stub, not the PDF;
    # treat it as unknown rather than reporting a misleading "0.0 MB".
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200, headers={"content-length": "8000"}
                )
            )
        ),
    )
    assert resolver.probe_pdf_size(paper_factory()) is None


def test_landing_urls_fail_fast_without_backends(monkeypatch):
    # A publisher landing URL is deterministically unresolvable; it
    # must get the permanent hint even when search backends are down
    # (no backend call at all).
    def never(q, max_results):
        raise AssertionError("search backend must not be called")

    monkeypatch.setattr(openalex, "search", never)
    monkeypatch.setattr(arxiv, "search", never)
    with pytest.raises(ValueError, match="publisher landing URLs"):
        resolver.resolve("https://www.nature.com/articles/s41586-020-2649-2")


def test_download_any_transient_failure_wins_classification(
    monkeypatch, paper_factory
):
    # bioRxiv 503 then medRxiv 404 (the mirror's guaranteed miss): the
    # transient failure must win, or a blip earns a permanent tag.
    from paperboy.models import Paper

    paper = Paper(
        title="BioRxiv Flaky",
        authors=[],
        abstract="",
        published="2024",
        url="u",
        pdf_url=None,
        doi="10.1101/2024.01.01.573000",
    )

    def route(request):
        if "biorxiv.org" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(404)

    monkeypatch.setattr(
        resolver, "client", httpx.Client(transport=httpx.MockTransport(route))
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        resolver.download_pdf(paper)
    assert excinfo.value.response.status_code == 503


# --- honest URL classification -----------------------------------------


def test_direct_pdf_url_suggests_attach_pdf(monkeypatch):
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="direct PDF link"):
        resolver.resolve("https://stat.cmu.edu/~cshalizi/ADAfaEPoV/ADA.pdf")


def test_github_url_is_a_file_share(monkeypatch):
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="file-share or code-host"):
        resolver.resolve("https://github.com/user/repo/blob/main/primer.pdf")


def test_google_drive_url_is_a_file_share(monkeypatch):
    with pytest.raises(ValueError, match="file-share or code-host"):
        resolver.resolve("https://drive.google.com/file/d/abc123/view")


def test_amazon_url_suggests_add_book(monkeypatch):
    with pytest.raises(ValueError, match="add_book"):
        resolver.resolve("https://www.amazon.com/dp/1470421992")


# --- Semantic Scholar title fallback -----------------------------------


def test_title_falls_back_to_semantic_scholar(monkeypatch, paper_factory):
    wanted = paper_factory(
        title="A Textbook OpenAlex Ranks Poorly",
        arxiv_id=None,
        doi="10.1/textbook",
    )
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    monkeypatch.setattr(s2, "search_title", lambda q, limit=5: [wanted])
    assert resolver.resolve("A Textbook OpenAlex Ranks Poorly") is wanted


# --- "did you mean" for plausible-but-not-confident titles --------------


def test_plausible_title_offers_candidate_with_id(monkeypatch, paper_factory):
    # Ratio ~0.77: below auto-accept, above the offer floor.
    candidate = paper_factory(
        title="Attention Is Not All You Really Need Now",
        arxiv_id=None,
        doi="10.1/candidate",
    )
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [candidate])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(resolver.AmbiguousTitleError) as excinfo:
        resolver.resolve("Attention Is All You Need")
    message = str(excinfo.value)
    assert "Did you mean" in message
    assert "10.1/candidate" in message
    # It subclasses ValueError so every existing caller degrades safely.
    assert isinstance(excinfo.value, ValueError)


def test_plausible_title_without_id_does_not_offer(monkeypatch, paper_factory):
    # Same mid-band match, but the candidate has no concrete id to re-run
    # with — offering it would loop forever, so it hard-fails instead.
    candidate = paper_factory(
        title="Attention Is Not All You Really Need Now",
        arxiv_id=None,
        doi=None,
    )
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [candidate])
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="confidently-matching title") as info:
        resolver.resolve("Attention Is All You Need")
    assert not isinstance(info.value, resolver.AmbiguousTitleError)


def test_transient_error_wins_over_offer(monkeypatch, paper_factory):
    # A 429 on one arm plus a mid-band candidate on another must raise the
    # transient error, not offer a possibly-inferior match.
    candidate = paper_factory(
        title="Attention Is Not All You Really Need Now",
        arxiv_id=None,
        doi="10.1/candidate",
    )
    request = httpx.Request("GET", "https://api.openalex.org/works")

    def throttled(q, max_results):
        raise httpx.HTTPStatusError(
            "429",
            request=request,
            response=httpx.Response(429, request=request),
        )

    monkeypatch.setattr(openalex, "search", throttled)
    monkeypatch.setattr(arxiv, "search", lambda q, max_results: [candidate])
    with pytest.raises(httpx.HTTPStatusError):
        resolver.resolve("Attention Is All You Need")


# --- review-round fixes -------------------------------------------------


def test_classifier_uses_domain_suffixes_not_substrings():
    # amazon.science hosts papers; notgithub.com is not GitHub; path
    # segments like /bookmark are not /book. All must get the generic
    # hint, not a wrong specific one.
    for url in (
        "https://www.amazon.science/publications/x",
        "https://notgithub.com/x",
        "https://example.com/bookmark/123",
        "https://pubs.example.org/booklets/wp17.html",
    ):
        assert "publisher landing URLs" in resolver._classify_url(url)
    # The real cases still classify.
    assert "add_book" in resolver._classify_url(
        "https://www.amazon.com/dp/1470421992"
    )
    assert "add_book" in resolver._classify_url(
        "https://bookstore.ams.org/stml-78/"
    )
    assert "add_book" in resolver._classify_url(
        "https://global.oup.com/academic/book/9780198522195"
    )
    assert "file-share" in resolver._classify_url(
        "https://github.com/user/repo/blob/main/x.pdf"
    )


def test_fetch_pdf_rejects_content_length_over_cap(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    headers={"content-length": str(10**10)},
                    content=b"%PDF-1.7",
                )
            )
        ),
    )
    with pytest.raises(ValueError, match="larger than"):
        resolver.fetch_pdf("https://host/huge.pdf")


def test_fetch_pdf_rejects_oversized_body_without_length(monkeypatch):
    big = b"%PDF-" + b"0" * (resolver._MAX_FETCH_BYTES + 10)
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=big)
            )
        ),
    )
    with pytest.raises(ValueError, match="larger than"):
        resolver.fetch_pdf("https://host/huge.pdf")


def test_fetch_pdf_streams_valid_pdf(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"%PDF-1.7 body")
            )
        ),
    )
    assert resolver.fetch_pdf("https://host/x.pdf") == b"%PDF-1.7 body"


def test_fetch_pdf_rejects_html_early(monkeypatch):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    200,
                    content=b"<html>" + b"x" * 5000,
                    headers={"content-type": "text/html"},
                )
            )
        ),
    )
    with pytest.raises(ValueError, match="non-PDF content"):
        resolver.fetch_pdf("https://host/notpdf")
