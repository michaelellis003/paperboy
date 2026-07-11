import pytest

from paperboy import arxiv, delivery, openalex, resolver, server, zotero_client


@pytest.fixture
def sent_documents(monkeypatch):
    sent = []

    def fake_send(documents):
        sent.extend(documents)
        return f"Sent {len(documents)} document(s)"

    monkeypatch.setattr(delivery, "send_documents", fake_send)
    return sent


@pytest.fixture
def no_download(monkeypatch):
    monkeypatch.setattr(resolver, "download_pdf", lambda paper: b"%PDF")


def test_search_papers_uses_openalex_by_default(
    env, monkeypatch, paper_factory
):
    monkeypatch.setattr(
        openalex, "search", lambda q, max_results: [paper_factory()]
    )
    results = server.search_papers("attention")
    assert results[0]["ref"] == "2401.12345"
    assert results[0]["open_access_pdf"] is True


def test_search_papers_arxiv_source(env, monkeypatch, paper_factory):
    monkeypatch.setattr(
        arxiv,
        "search",
        lambda q, max_results: [paper_factory(pdf_url=None, arxiv_id=None)],
    )
    results = server.search_papers("attention", source="arxiv")
    assert results[0]["ref"] == "https://arxiv.org/abs/2401.12345"
    assert results[0]["open_access_pdf"] is False


def test_send_papers_sends_and_reports(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    good = paper_factory()
    paywalled = paper_factory(
        title="Paywalled", pdf_url=None, arxiv_id=None, doi="10.1/x"
    )
    papers = iter([good, paywalled])
    monkeypatch.setattr(resolver, "resolve", lambda ref: next(papers))
    receipt = server.send_papers(["2401.12345", "10.1/x"])
    assert len(sent_documents) == 1
    assert "No open-access PDF, not sent: Paywalled" in receipt


def test_send_papers_all_paywalled(env, monkeypatch, paper_factory):
    monkeypatch.setattr(
        resolver, "resolve", lambda ref: paper_factory(pdf_url=None)
    )
    receipt = server.send_papers(["10.1/x"])
    assert receipt.startswith("Nothing was sent")
    assert "no open-access PDF" in receipt


def test_send_papers_reports_unresolvable(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    def fake_resolve(ref):
        if ref == "gibberish":
            raise ValueError("nope")
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_papers(["2401.12345", "gibberish"])
    assert len(sent_documents) == 1
    assert "Could not resolve: gibberish" in receipt


def test_send_papers_nothing_resolves(env, monkeypatch):
    def fail(ref):
        raise ValueError("nope")

    monkeypatch.setattr(resolver, "resolve", fail)
    receipt = server.send_papers(["gibberish"])
    assert receipt == "Nothing was sent. could not resolve: gibberish"


def test_send_papers_records_in_zotero(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    env.setenv("ZOTERO_API_KEY", "k")
    env.setenv("ZOTERO_LIBRARY_ID", "1")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    added, marked = [], []
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client, "add_paper", lambda p: added.append(p) or "KEY"
    )
    monkeypatch.setattr(zotero_client, "mark_sent", marked.append)
    receipt = server.send_papers(["2401.12345"])
    assert added and marked == ["KEY"]
    assert "recorded in Zotero" in receipt


def test_queue_papers(env, monkeypatch, paper_factory):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", lambda p: "KEY")
    receipt = server.queue_papers(["2401.12345"])
    assert "Queued 1 paper(s) in 'Reading Queue'" in receipt


def test_queue_papers_reports_unresolvable(env, monkeypatch, paper_factory):
    def fake_resolve(ref):
        if ref == "gibberish":
            raise ValueError("nope")
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    monkeypatch.setattr(zotero_client, "add_paper", lambda p: "KEY")
    receipt = server.queue_papers(["2401.12345", "gibberish"])
    assert "Queued 1 paper(s)" in receipt
    assert "Could not resolve: gibberish" in receipt


def test_queue_papers_nothing_resolves(env, monkeypatch):
    def fail(ref):
        raise ValueError("nope")

    monkeypatch.setattr(resolver, "resolve", fail)
    receipt = server.queue_papers(["gibberish"])
    assert receipt.startswith("Nothing was queued")


def test_send_queue_empty(env, monkeypatch):
    monkeypatch.setattr(zotero_client, "unsent_queue_items", list)
    assert "empty" in server.send_queue()


def test_send_queue_mixed_items(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    items = [
        {"key": "GOOD", "data": {"url": "https://arxiv.org/abs/2401.12345"}},
        {"key": "NOREF", "data": {"title": "No Ref"}},
        {"key": "BROKEN", "data": {"title": "Broken", "url": "junk"}},
        {"key": "PAYWALL", "data": {"title": "Paywalled", "DOI": "10.1/x"}},
    ]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    marked = []
    monkeypatch.setattr(zotero_client, "mark_sent", marked.append)

    def fake_resolve(ref):
        if ref == "junk":
            raise ValueError("nope")
        if ref == "10.1/x":
            return paper_factory(pdf_url=None)
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_queue()
    assert marked == ["GOOD"]
    assert "No Ref (no DOI or URL)" in receipt
    assert "Broken (unresolvable: junk)" in receipt
    assert "Paywalled (no open-access PDF)" in receipt


def test_send_queue_nothing_deliverable(env, monkeypatch):
    items = [{"key": "NOREF", "data": {"title": "No Ref"}}]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    receipt = server.send_queue()
    assert receipt.startswith("Nothing in the queue is deliverable")


def test_bearer_auth_requires_token(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN"):
        server._bearer_auth()


def test_bearer_auth_rejects_short_token(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    with pytest.raises(SystemExit):
        server._bearer_auth()


def test_bearer_auth_accepts_long_token(monkeypatch):
    token = "x" * 43
    monkeypatch.setenv("MCP_AUTH_TOKEN", token)
    verifier = server._bearer_auth()
    assert token in verifier.tokens
