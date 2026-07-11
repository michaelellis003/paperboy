import httpx

from paperboy import s2

REC = {
    "recommendedPapers": [
        {
            "paperId": "abc123",
            "title": "A Recommended Paper",
            "abstract": "Something related.",
            "year": 2026,
            "publicationDate": "2026-06-15",
            "authors": [{"name": "Grace Hopper"}, {"name": None}],
            "externalIds": {"ArXiv": "2606.16093", "CorpusId": 1},
            "openAccessPdf": {"url": ""},
        },
        {
            "paperId": "def456",
            "title": "A DOI-Only Paper",
            "abstract": None,
            "year": 2020,
            "publicationDate": None,
            "authors": [],
            "externalIds": {"DOI": "10.1000/rec"},
            "openAccessPdf": {"url": "https://oa.example/p.pdf"},
        },
        {
            "paperId": "ghi789",
            "title": None,
            "externalIds": None,
            "openAccessPdf": None,
        },
    ]
}


def _client(captured):
    def handler(request):
        captured["params"] = dict(request.url.params)
        captured["body"] = request.read().decode()
        return httpx.Response(200, json=REC)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_recommend_parses_and_sends_seeds(monkeypatch):
    captured = {}
    monkeypatch.setattr(s2, "client", _client(captured))
    papers = s2.recommend(
        ["ArXiv:2312.00752", "DOI:10.1/x"], pool="recent", limit=5
    )
    assert "ArXiv:2312.00752" in captured["body"]
    assert captured["params"]["from"] == "recent"

    arxiv_paper = papers[0]
    assert arxiv_paper.arxiv_id == "2606.16093"
    assert arxiv_paper.pdf_url == "https://arxiv.org/pdf/2606.16093"
    assert arxiv_paper.published == "2026-06-15"
    assert arxiv_paper.authors == ["Grace Hopper"]

    doi_paper = papers[1]
    assert doi_paper.doi == "10.1000/rec"
    assert doi_paper.pdf_url == "https://oa.example/p.pdf"
    assert doi_paper.published == "2020"
    assert doi_paper.url == "https://doi.org/10.1000/rec"

    bare = papers[2]
    assert bare.title == "(untitled)"
    assert bare.pdf_url is None
    assert "semanticscholar.org" in bare.url
