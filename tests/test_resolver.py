import httpx
import pytest

from paperboy import arxiv, doi, openalex, resolver


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
