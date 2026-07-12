from paperboy.models import Paper, clean_title


def test_clean_title_strips_markup_and_spacing():
    assert clean_title("<i>Colloquium</i>: Topological insulators") == (
        "Colloquium: Topological insulators"
    )
    assert clean_title("<b>Bold</b> <i>and</i> italic") == "Bold and italic"
    # tag removal can leave a space before punctuation
    assert clean_title("<i>Colloquium</i> : X") == "Colloquium: X"
    assert clean_title("plain  title") == "plain title"


def test_paper_cleans_title_for_every_source():
    # The model cleans on construction, so DOI/Crossref, S2, arXiv, and
    # OpenAlex all get clean titles without each remembering to call it.
    paper = Paper(
        title="<i>Colloquium</i>: Topological insulators",
        authors=["A"],
        abstract="x",
        published="2010",
        url="https://doi.org/10.1103/RevModPhys.82.3045",
        pdf_url=None,
        doi="10.1103/RevModPhys.82.3045",
    )
    assert paper.title == "Colloquium: Topological insulators"
