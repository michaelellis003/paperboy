from paperboy.models import Paper, biorxiv_pdf_urls, clean_title


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


def test_biorxiv_doi_gets_pdf_url_when_oa_index_is_blank():
    # bioRxiv is open by definition; a missing OA record must not leave
    # the paper flagged as having no PDF (round-7 false negative).
    paper = Paper(
        title="A preprint",
        authors=["A"],
        abstract="x",
        published="2021",
        url="https://doi.org/10.1101/2021.06.11.448104",
        pdf_url=None,
        doi="10.1101/2021.06.11.448104",
    )
    assert paper.pdf_url == (
        "https://www.biorxiv.org/content/10.1101/2021.06.11.448104.full.pdf"
    )


def test_non_preprint_doi_stays_without_pdf_url():
    paper = Paper(
        title="A paywalled paper",
        authors=["A"],
        abstract="x",
        published="2020",
        url="https://doi.org/10.1126/science.1125572",
        pdf_url=None,
        doi="10.1126/science.1125572",
    )
    assert paper.pdf_url is None


def test_biorxiv_pdf_urls_helper():
    assert biorxiv_pdf_urls("10.1038/x") == []
    assert biorxiv_pdf_urls(None) == []
    urls = biorxiv_pdf_urls("10.1101/abc")
    assert "biorxiv.org" in urls[0] and "medrxiv.org" in urls[1]
