import httpx
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


@pytest.fixture
def zotero_env(env, monkeypatch):
    env.setenv("ZOTERO_API_KEY", "k")
    env.setenv("ZOTERO_LIBRARY_ID", "1")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    monkeypatch.setattr(zotero_client, "find_item", lambda p: None)
    monkeypatch.setattr(zotero_client, "add_paper", lambda p: ("KEY", True))
    monkeypatch.setattr(zotero_client, "mark_sent", lambda k: None)
    monkeypatch.setattr(zotero_client, "mark_no_pdf", lambda k: None)
    return env


# --- search ----------------------------------------------------------------


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
    # DOI-less, non-arXiv works fall back to the exact title, which
    # round-trips through the resolver (a bare URL would not).
    assert results[0]["ref"] == "A Test Paper"
    assert results[0]["open_access_pdf"] is False


def test_search_max_results_floor_and_cap(env, monkeypatch):
    seen = []

    def fake_search(q, max_results):
        seen.append(max_results)
        return []

    monkeypatch.setattr(openalex, "search", fake_search)
    server.search_papers("q", max_results=0)
    server.search_papers("q", max_results=999)
    assert seen == [1, 25]


def test_search_wraps_backend_errors_actionably(env, monkeypatch):
    def boom(q, max_results):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(openalex, "search", boom)
    with pytest.raises(RuntimeError, match="Search failed"):
        server.search_papers("anything")


def test_search_trims_long_author_lists(env, monkeypatch, paper_factory):
    paper = paper_factory(authors=[f"Author {i}" for i in range(9)])
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [paper])
    result = server.search_papers("q")[0]
    assert result["authors"] == ["Author 0", "Author 1", "Author 2", "et al."]


def test_search_truncates_abstract(env, monkeypatch, paper_factory):
    paper = paper_factory(abstract="x" * 1000)
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [paper])
    assert len(server.search_papers("q")[0]["abstract"]) == 300


# --- send_papers -----------------------------------------------------------


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


def test_send_papers_all_paywalled_still_queues(
    zotero_env, monkeypatch, paper_factory
):
    added, tagged = [], []
    monkeypatch.setattr(
        resolver, "resolve", lambda ref: paper_factory(pdf_url=None)
    )
    monkeypatch.setattr(
        zotero_client, "add_paper", lambda p: (added.append(p), "K", True)[1:]
    )
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    receipt = server.send_papers(["10.1/x"])
    assert added and tagged == ["K"]
    assert "queued unsent in Zotero Reading Queue" in receipt


def test_send_papers_reports_unresolvable(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    def fake_resolve(ref):
        if ref == "gibberish":
            raise ValueError("could not resolve: gibberish")
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_papers(["2401.12345", "gibberish"])
    assert len(sent_documents) == 1
    assert "could not resolve: gibberish" in receipt


def test_send_papers_relays_resolver_hints(env, monkeypatch):
    def fail(ref):
        raise ValueError(
            f"Could not resolve {ref!r} (publisher landing URLs are "
            "not supported — try the DOI or exact title)"
        )

    monkeypatch.setattr(resolver, "resolve", fail)
    receipt = server.send_papers(["https://nature.com/articles/x"])
    assert "publisher landing URLs" in receipt


def test_send_papers_4xx_is_not_labeled_transient(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    request = httpx.Request("GET", "https://api.openalex.org/works")

    def fake_resolve(ref):
        if ref == "Weird Title?":
            raise httpx.HTTPStatusError(
                "400",
                request=request,
                response=httpx.Response(400, request=request),
            )
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_papers(["2401.12345", "Weird Title?"])
    assert "backend rejected the request: HTTP 400" in receipt
    assert "retry" not in receipt.split("HTTP 400")[1][:40]


def test_send_papers_distinguishes_network_errors(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    def fake_resolve(ref):
        if ref == "flaky":
            raise httpx.ReadTimeout("timed out")
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_papers(["2401.12345", "flaky"])
    assert len(sent_documents) == 1
    assert "temporarily unreachable" in receipt and "flaky" in receipt


def test_send_papers_nothing_resolves(env, monkeypatch):
    def fail(ref):
        raise ValueError(f"could not resolve: {ref}")

    monkeypatch.setattr(resolver, "resolve", fail)
    receipt = server.send_papers(["gibberish"])
    assert receipt == "Nothing was sent. could not resolve: gibberish"


def test_send_papers_dedupes_refs_in_call(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    server.send_papers(["2401.12345", "arXiv:2401.12345", "2401.12345"])
    assert len(sent_documents) == 1


def test_send_papers_dedupes_doi_and_arxiv_forms(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    # Same paper: DOI-resolved form has no arXiv id, arXiv form has no
    # DOI — only the normalized title connects them.
    doi_form = paper_factory(
        title="Observation of a New Particle!", arxiv_id=None, doi="10.1/h"
    )
    arxiv_form = paper_factory(
        title="Observation of a new particle", arxiv_id="1207.7214", doi=None
    )
    papers = iter([doi_form, arxiv_form])
    monkeypatch.setattr(resolver, "resolve", lambda ref: next(papers))
    server.send_papers(["10.1/h", "1207.7214"])
    assert len(sent_documents) == 1


def test_send_papers_skips_already_sent(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client, "find_item", lambda p: {"key": "K", "data": {}}
    )
    monkeypatch.setattr(zotero_client, "is_sent", lambda item: True)
    receipt = server.send_papers(["2401.12345"])
    assert sent_documents == []
    assert "Already sent" in receipt or "already sent" in receipt
    assert "force=True" in receipt


def test_send_papers_force_resends(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client, "find_item", lambda p: {"key": "K", "data": {}}
    )
    monkeypatch.setattr(zotero_client, "is_sent", lambda item: True)
    server.send_papers(["2401.12345"], force=True)
    assert len(sent_documents) == 1


def test_send_papers_survives_dead_pdf_link(
    env, monkeypatch, paper_factory, sent_documents
):
    good = paper_factory()
    dead = paper_factory(
        title="Dead Link",
        arxiv_id=None,
        doi="10.1/dead",
        pdf_url="https://mirror.invalid/x.pdf",
    )
    papers = iter([good, dead])
    monkeypatch.setattr(resolver, "resolve", lambda ref: next(papers))

    def fake_download(paper):
        if paper.title == "Dead Link":
            raise ValueError("Could not download PDF: 404")
        return b"%PDF"

    monkeypatch.setattr(resolver, "download_pdf", fake_download)
    receipt = server.send_papers(["2401.12345", "10.1/dead"])
    assert len(sent_documents) == 1
    assert "download failed: Dead Link" in receipt


def test_send_papers_records_in_zotero(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    added, marked = [], []

    def fake_add(paper):
        added.append(paper)
        return "KEY", True

    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", fake_add)
    monkeypatch.setattr(zotero_client, "mark_sent", marked.append)
    receipt = server.send_papers(["2401.12345"])
    assert added and marked == ["KEY"]
    assert "recorded in Zotero" in receipt


def test_send_papers_dry_run(env, monkeypatch, paper_factory):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(resolver, "probe_pdf_size", lambda p: 2_500_000)
    receipt = server.send_papers(["2401.12345"], dry_run=True)
    assert "Would send 1 paper(s), ~2.5 MB" in receipt
    assert "A Test Paper (2.5 MB)" in receipt


def test_send_papers_dry_run_unknown_size(env, monkeypatch, paper_factory):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(resolver, "probe_pdf_size", lambda p: None)
    receipt = server.send_papers(["2401.12345"], dry_run=True)
    assert "size unknown" in receipt
    # the headline total must not silently count unknowns as zero
    assert "+ 1 of unknown size" in receipt


# --- chunking --------------------------------------------------------------


def test_chunk_splits_on_attachment_count():
    docs = [(f"p{i}.pdf", b"x") for i in range(26)]
    batches = server._chunk(docs)
    assert [len(b) for b in batches] == [25, 1]


def test_chunk_splits_on_total_size():
    docs = [
        ("a.pdf", b"x" * 30_000_000),
        ("b.pdf", b"x" * 30_000_000),
    ]
    batches = server._chunk(docs)
    assert len(batches) == 2


def test_deliver_reports_partial_failure(env, monkeypatch):
    calls = []

    def fake_send(batch):
        calls.append(batch)
        if len(calls) == 2:
            raise delivery.DeliveryError("boom")
        return "Sent batch"

    monkeypatch.setattr(delivery, "send_documents", fake_send)
    docs = [(f"p{i}.pdf", b"x" * 30_000_000) for i in range(2)]
    receipt, delivered = server._deliver(docs)
    assert "Sent batch" in receipt and "delivery failed: boom" in receipt
    assert delivered == {"p0.pdf"}


def test_partial_delivery_marks_only_sent_batch(
    zotero_env, monkeypatch, paper_factory, no_download
):
    big = paper_factory(title="Big", arxiv_id="1111.1")
    small = paper_factory(title="Small", arxiv_id="2222.2")
    papers = iter([big, small])
    monkeypatch.setattr(resolver, "resolve", lambda ref: next(papers))
    monkeypatch.setattr(resolver, "download_pdf", lambda p: b"x" * 30_000_000)
    calls = []

    def fake_send(batch):
        calls.append(batch)
        if len(calls) == 2:
            raise delivery.DeliveryError("smtp died")
        return "Sent batch"

    monkeypatch.setattr(delivery, "send_documents", fake_send)
    marked = []
    monkeypatch.setattr(zotero_client, "mark_sent", marked.append)
    receipt = server.send_papers(["1111.1", "2222.2"])
    assert marked == ["KEY"]  # only the delivered batch's paper
    assert "delivery failed: smtp died" in receipt


# --- queue_papers ----------------------------------------------------------


@pytest.fixture
def zotero_ok(monkeypatch):
    monkeypatch.setattr(zotero_client, "ensure_configured", lambda: None)


def test_queue_papers(env, zotero_ok, monkeypatch, paper_factory):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", lambda p: ("KEY", True))
    receipt = server.queue_papers(["2401.12345"])
    assert "Queued 1 new paper(s) in 'Reading Queue'" in receipt


def test_queue_papers_reports_existing(
    env, zotero_ok, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", lambda p: ("KEY", False))
    receipt = server.queue_papers(["2401.12345"])
    assert "Queued 0 new paper(s)" in receipt
    assert "already in queue: A Test Paper" in receipt


def test_queue_papers_nothing_resolves(env, zotero_ok, monkeypatch):
    def fail(ref):
        raise ValueError("nope")

    monkeypatch.setattr(resolver, "resolve", fail)
    receipt = server.queue_papers(["gibberish"])
    assert receipt.startswith("Nothing was queued")


def test_queue_papers_tags_no_pdf_at_queue_time(
    env, zotero_ok, monkeypatch, paper_factory
):
    tagged = []
    monkeypatch.setattr(
        resolver, "resolve", lambda ref: paper_factory(pdf_url=None)
    )
    monkeypatch.setattr(zotero_client, "add_paper", lambda p: ("KEY", True))
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    receipt = server.queue_papers(["10.1/x"])
    assert tagged == ["KEY"]
    assert "no open-access PDF (won't be auto-sent)" in receipt


def test_queue_papers_fails_fast_without_zotero(env, monkeypatch):
    def explode(ref):
        raise AssertionError("resolver must not be called")

    monkeypatch.setattr(resolver, "resolve", explode)
    with pytest.raises(RuntimeError, match="paperboy setup"):
        server.queue_papers(["2401.12345"])


# --- list / remove ---------------------------------------------------------


def test_list_queue_tool(env, monkeypatch):
    entries = [{"title": "T", "ref": "10.1/x", "status": "unsent", "added": ""}]
    monkeypatch.setattr(zotero_client, "list_queue", lambda: entries)
    assert server.list_queue() == entries


def test_remove_from_queue_tool(env, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "remove_by_refs",
        lambda refs: ([{"title": "Paper A", "was_sent": False}], ["nope"]),
    )
    receipt = server.remove_from_queue(["10.1/a", "nope"])
    assert "Removed 1 item(s)" in receipt and "Paper A" in receipt
    assert "Not found in queue: nope" in receipt
    assert "re-deliver" not in receipt


def test_remove_from_queue_warns_on_sent_items(env, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "remove_by_refs",
        lambda refs: ([{"title": "Read One", "was_sent": True}], []),
    )
    receipt = server.remove_from_queue(["10.1/a"])
    assert "sent-state is erased" in receipt
    assert "WILL re-deliver: Read One" in receipt


def test_setup_status_flags_invalid_delivery_method(env):
    env.setenv("DELIVERY_METHOD", "carrier-pigeon")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    status = server.setup_status()
    assert status["delivery_ready"] is False
    assert any("carrier-pigeon" in step for step in status["next_steps"])


# --- send_queue ------------------------------------------------------------


def test_send_queue_empty(env, monkeypatch):
    monkeypatch.setattr(zotero_client, "unsent_queue_items", list)
    assert "empty" in server.send_queue()


def test_send_queue_mixed_items(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    items = [
        {"key": "GOOD", "data": {"url": "https://arxiv.org/abs/2401.12345"}},
        {
            "key": "TITLEONLY",
            "data": {"title": "Title Only Paper", "url": "junk-landing"},
        },
        {"key": "BROKEN", "data": {"title": "Broken", "url": "junk"}},
        {"key": "PAYWALL", "data": {"title": "Paywalled", "DOI": "10.1/x"}},
    ]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    marked, no_pdf_tagged = [], []
    monkeypatch.setattr(zotero_client, "mark_sent", marked.append)
    monkeypatch.setattr(zotero_client, "mark_no_pdf", no_pdf_tagged.append)

    def fake_resolve(ref):
        if ref in ("junk", "junk-landing", "Broken"):
            raise ValueError("nope")
        if ref == "10.1/x":
            return paper_factory(pdf_url=None)
        if ref == "Title Only Paper":
            return paper_factory(title="Title Only Paper", arxiv_id="3333.3")
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_queue()
    # the landing-URL-only item is rescued by its stored title
    assert marked == ["GOOD", "TITLEONLY"]
    assert no_pdf_tagged == ["PAYWALL"]
    assert "Broken (unresolvable: junk)" in receipt
    assert "Paywalled (no open-access PDF — won't retry)" in receipt


def test_send_queue_nothing_deliverable(env, monkeypatch):
    items = [{"key": "X", "data": {"title": "Unfindable"}}]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda ref: (_ for _ in ()).throw(ValueError("nope")),
    )
    receipt = server.send_queue()
    assert receipt.startswith("Nothing in the queue is deliverable")
    assert "Unfindable (unresolvable: Unfindable)" in receipt


# --- setup_status ----------------------------------------------------------


def test_setup_status_unconfigured(monkeypatch):
    import paperboy.config

    for var in list(__import__("os").environ):
        if var.startswith(("SMTP", "DEVICE", "KINDLE", "ZOTERO", "DROPBOX")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(paperboy.config, "_settings", None)
    status = server.setup_status()
    assert status["delivery_ready"] is False
    assert status["zotero_configured"] is False
    assert any("paperboy setup" in step for step in status["next_steps"])


def test_setup_status_email_ready(env):
    status = server.setup_status()
    assert status["delivery_method"] == "email"
    assert status["delivery_ready"] is True
    assert status["email_backend_configured"] is True
    assert status["dropbox_backend_configured"] is False
    assert status["open_access_lookup_ready"] is True


def test_setup_status_warns_without_polite_email(env):
    env.setenv("FROM_EMAIL", "")
    env.setenv("CONTACT_EMAIL", "")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    status = server.setup_status()
    assert status["open_access_lookup_ready"] is False
    assert any("CONTACT_EMAIL" in step for step in status["next_steps"])


def test_no_pdf_receipt_hints_when_lookup_disabled(
    env, monkeypatch, paper_factory
):
    env.setenv("FROM_EMAIL", "")
    env.setenv("CONTACT_EMAIL", "")
    import paperboy.config

    env.setattr(paperboy.config, "_settings", None)
    monkeypatch.setattr(
        resolver, "resolve", lambda ref: paper_factory(pdf_url=None)
    )
    receipt = server.send_papers(["10.1/x"])
    assert "open-access PDF lookup is disabled" in receipt
    assert "CONTACT_EMAIL" in receipt


# --- auth ------------------------------------------------------------------


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
