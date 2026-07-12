import httpx
import pytest

from paperboy import doi

CROSSREF = {
    "message": {
        "title": ["Ten  Simple\nRules"],
        "author": [
            {"given": "Geir", "family": "Sandve"},
            {"name": "The Consortium"},
        ],
        "abstract": "<jats:p>An abstract.</jats:p>",
        "issued": {"date-parts": [[2013, 10, 24]]},
        "URL": "https://doi.org/10.1371/journal.pcbi.1003285",
    }
}


def _client(crossref_status=200, unpaywall_body=None, unpaywall_status=200):
    def handler(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(crossref_status, json=CROSSREF)
        assert request.url.params["email"] == "user@example.com"
        return httpx.Response(unpaywall_status, json=unpaywall_body or {})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("10.1371/journal.pcbi.1003285", "10.1371/journal.pcbi.1003285"),
        ("doi:10.1038/nature12373", "10.1038/nature12373"),
        ("https://doi.org/10.1038/nature12373", "10.1038/nature12373"),
        ("see 10.1038/nature12373.", "10.1038/nature12373"),
        ("https://arxiv.org/abs/2401.12345", None),
        ("plain text", None),
    ],
)
def test_extract_doi(ref, expected):
    assert doi.extract_doi(ref) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("10.48550/arXiv.2005.14165", "2005.14165"),
        ("10.48550/ARXIV.2005.14165", "2005.14165"),
        ("10.1038/nature12373", None),
    ],
)
def test_arxiv_id_from_doi(value, expected):
    assert doi.arxiv_id_from_doi(value) == expected


def test_get_paper_parses_crossref(env, monkeypatch):
    unpaywall = {
        "best_oa_location": {"url_for_pdf": "https://example.org/p.pdf"}
    }
    monkeypatch.setattr(doi, "client", _client(unpaywall_body=unpaywall))
    paper = doi.get_paper("10.1371/journal.pcbi.1003285")
    assert paper.title == "Ten Simple Rules"
    assert paper.authors == ["Geir Sandve", "The Consortium"]
    assert paper.abstract == "An abstract."
    assert paper.published == "2013-10-24"
    assert paper.doi == "10.1371/journal.pcbi.1003285"
    assert paper.pdf_url == "https://example.org/p.pdf"


def test_get_paper_unknown_doi(env, monkeypatch):
    monkeypatch.setattr(doi, "client", _client(crossref_status=404))
    with pytest.raises(ValueError, match="not found in Crossref"):
        doi.get_paper("10.9999/nope")


def test_no_oa_pdf_resolves_without_pdf(env, monkeypatch):
    monkeypatch.setattr(
        doi, "client", _client(unpaywall_body={"best_oa_location": None})
    )
    paper = doi.get_paper("10.1371/journal.pcbi.1003285")
    assert paper.pdf_url is None


def test_oa_pdf_falls_back_to_other_locations(env, monkeypatch):
    unpaywall = {
        "best_oa_location": {"url_for_pdf": None},
        "oa_locations": [{"url_for_pdf": "https://backup.org/p.pdf"}],
    }
    monkeypatch.setattr(doi, "client", _client(unpaywall_body=unpaywall))
    paper = doi.get_paper("10.1371/journal.pcbi.1003285")
    assert paper.pdf_url == "https://backup.org/p.pdf"


def test_unpaywall_404_means_no_pdf(env, monkeypatch):
    # 404 is Unpaywall's definitive "no record for this DOI".
    monkeypatch.setattr(doi, "client", _client(unpaywall_status=404))
    paper = doi.get_paper("10.1371/journal.pcbi.1003285")
    assert paper.pdf_url is None


def test_unpaywall_outage_is_not_a_no_pdf_verdict(env, monkeypatch):
    # 429/5xx/422 are Unpaywall's problem, not a fact about the paper.
    # Swallowing them would permanently tag an OA paper no-oa-pdf.
    import httpx
    import pytest

    monkeypatch.setattr(doi, "client", _client(unpaywall_status=503))
    with pytest.raises(httpx.HTTPStatusError):
        doi.get_paper("10.1371/journal.pcbi.1003285")


def test_no_polite_email_skips_unpaywall_entirely(env, monkeypatch):
    env.setenv("FROM_EMAIL", "")
    env.setenv("CONTACT_EMAIL", "")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)

    def handler(request):
        if request.url.host == "api.crossref.org":
            return httpx.Response(200, json=CROSSREF)
        raise AssertionError("Unpaywall must not be called without email")

    monkeypatch.setattr(
        doi, "client", httpx.Client(transport=httpx.MockTransport(handler))
    )
    paper = doi.get_paper("10.1371/journal.pcbi.1003285")
    assert paper.pdf_url is None
