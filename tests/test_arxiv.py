import httpx
import pytest

from paperboy import arxiv

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2401.12345v2</id>
    <title>A  Test
 Paper</title>
    <summary>An abstract.</summary>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <published>2024-01-20T12:00:00Z</published>
    <arxiv:doi>10.1016/j.example.2024.01</arxiv:doi>
  </entry>
</feed>"""

EMPTY_FEED = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
)


def _client(xml: str) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=xml)
        )
    )


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("2401.12345", "2401.12345"),
        ("arXiv:2401.12345v2", "2401.12345"),
        ("arxiv:2401.12345", "2401.12345"),
        ("https://arxiv.org/abs/2401.12345", "2401.12345"),
        ("https://arxiv.org/pdf/2401.12345.pdf", "2401.12345"),
        ("https://arxiv.org/abs/2401.12345?context=cs", "2401.12345"),
        ("https://arxiv.org/abs/2401.12345#section", "2401.12345"),
        ("math.GT/0309136", "math.GT/0309136"),
    ],
)
def test_normalize_id(ref, expected):
    assert arxiv.normalize_id(ref) == expected


def test_normalize_id_rejects_garbage():
    with pytest.raises(ValueError, match="Could not parse"):
        arxiv.normalize_id("not a paper")


def test_get_paper_parses_entry(monkeypatch):
    monkeypatch.setattr(arxiv, "client", _client(ATOM))
    paper = arxiv.get_paper("2401.12345")
    assert paper.title == "A Test Paper"
    assert paper.authors == ["Ada Lovelace", "Alan Turing"]
    assert paper.published == "2024-01-20"
    assert paper.arxiv_id == "2401.12345"
    assert paper.url == "https://arxiv.org/abs/2401.12345"
    assert paper.pdf_url == "https://arxiv.org/pdf/2401.12345"
    assert paper.safe_filename == "A_Test_Paper_2401.12345.pdf"
    # arXiv reports the journal DOI so DOI/arXiv forms deduplicate
    assert paper.doi == "10.1016/j.example.2024.01"


def test_get_paper_not_found(monkeypatch):
    monkeypatch.setattr(arxiv, "client", _client(EMPTY_FEED))
    with pytest.raises(ValueError, match="not found"):
        arxiv.get_paper("2401.99999")


def test_search_parses_entries(monkeypatch):
    monkeypatch.setattr(arxiv, "client", _client(ATOM))
    results = arxiv.search("attention", max_results=3)
    assert len(results) == 1
    assert results[0].arxiv_id == "2401.12345"
