import httpx

from paperboy import openalex

WORK = {
    "id": "https://openalex.org/W1",
    "display_name": "Attention Is All You Need",
    "publication_date": "2017-06-12",
    "doi": "https://doi.org/10.48550/arxiv.1706.03762",
    "authorships": [
        {"author": {"display_name": "Ashish Vaswani"}},
        {"author": {}},
    ],
    "abstract_inverted_index": {"dominant": [2], "The": [0], "sequence": [1]},
    "best_oa_location": {
        "pdf_url": "https://arxiv.org/pdf/1706.03762",
        "landing_page_url": "https://arxiv.org/abs/1706.03762",
    },
    "primary_location": {
        "landing_page_url": "https://arxiv.org/abs/1706.03762v7"
    },
}


def _client(results):
    def handler(request):
        assert request.url.params["mailto"] == "user@example.com"
        return httpx.Response(200, json={"results": results})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_search_parses_work(env, monkeypatch):
    monkeypatch.setattr(openalex, "client", _client([WORK]))
    papers = openalex.search("attention", max_results=1)
    assert len(papers) == 1
    paper = papers[0]
    assert paper.title == "Attention Is All You Need"
    assert paper.authors == ["Ashish Vaswani"]
    assert paper.abstract == "The sequence dominant"
    assert paper.doi == "10.48550/arxiv.1706.03762"
    assert paper.arxiv_id == "1706.03762"
    assert paper.pdf_url == "https://arxiv.org/pdf/1706.03762"


def test_search_strips_wildcards_from_query(env, monkeypatch):
    def handler(request):
        # OpenAlex 400s on wildcard chars; they must never reach it
        assert "?" not in request.url.params["search"]
        assert "*" not in request.url.params["search"]
        return httpx.Response(200, json={"results": []})

    monkeypatch.setattr(
        openalex,
        "client",
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    openalex.search("Do Vision Transformers See Like CNNs?")
    openalex.search("wildcard * query")


def test_arxiv_id_strips_pdf_suffix(env, monkeypatch):
    work = {
        "id": "https://openalex.org/W3",
        "display_name": "Old Style",
        "primary_location": {
            "landing_page_url": "https://arxiv.org/pdf/1111.4246.pdf"
        },
    }
    monkeypatch.setattr(openalex, "client", _client([work]))
    paper = openalex.search("q")[0]
    assert paper.arxiv_id == "1111.4246"


def test_search_handles_sparse_work(env, monkeypatch):
    sparse = {"id": "https://openalex.org/W2"}
    monkeypatch.setattr(openalex, "client", _client([sparse]))
    paper = openalex.search("anything")[0]
    assert paper.title == "(untitled)"
    assert paper.authors == []
    assert paper.abstract == ""
    assert paper.pdf_url is None
    assert paper.doi is None
    assert paper.url == "https://openalex.org/W2"
