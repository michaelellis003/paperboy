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
    hit = paper_factory(title="Attention Is All You Need")
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [hit])
    assert resolver.resolve("attention is all you need") is hit


def test_title_with_weak_match_rejected(monkeypatch, paper_factory):
    hit = paper_factory(title="Something Entirely Different")
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [hit])
    with pytest.raises(ValueError, match="confidently-matching title"):
        resolver.resolve("attention is all you need")


def test_unresolvable_ref_raises(monkeypatch):
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [])
    with pytest.raises(ValueError, match="arXiv id, DOI, or"):
        resolver.resolve("my cool paper")


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
        resolver.download_pdf(paper_factory(pdf_url=None))
