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
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "created"),
    )
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


def test_search_rejects_empty_query(env):
    with pytest.raises(ValueError, match="non-empty query"):
        server.search_papers("   ")


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


def test_search_truncates_abstract_at_word_boundary(
    env, monkeypatch, paper_factory
):
    paper = paper_factory(abstract="word " * 200)
    monkeypatch.setattr(openalex, "search", lambda q, max_results: [paper])
    abstract = server.search_papers("q")[0]["abstract"]
    assert len(abstract) <= 304
    assert abstract.endswith(" ...")
    assert not abstract.removesuffix(" ...").endswith("wor")  # no mid-word cut


def test_search_rate_limit_error_names_the_fix(env, monkeypatch):
    request = httpx.Request("GET", "https://api.openalex.org/works")

    def throttled(q, max_results):
        raise httpx.HTTPStatusError(
            "429",
            request=request,
            response=httpx.Response(429, request=request),
        )

    monkeypatch.setattr(openalex, "search", throttled)
    with pytest.raises(RuntimeError, match="rate-limiting"):
        server.search_papers("anything")


# --- recommend_papers -------------------------------------------------------


def test_recommend_requires_a_signal(env):
    with pytest.raises(RuntimeError, match="No discovery signal"):
        server.recommend_papers()


def test_recommend_seeds_from_library(zotero_env, monkeypatch, paper_factory):
    seen = {}
    monkeypatch.setattr(
        zotero_client, "seed_ids", lambda limit=10: ["ArXiv:2312.00752"]
    )
    monkeypatch.setattr(zotero_client, "known_identities", set)

    def fake_recommend(seed_ids, pool, limit):
        seen["seeds"], seen["pool"] = seed_ids, pool
        return [paper_factory(title="Fresh Rec", arxiv_id="2606.1")]

    monkeypatch.setattr(server.s2, "recommend", fake_recommend)
    result = server.recommend_papers()
    assert seen["seeds"] == ["ArXiv:2312.00752"]
    assert seen["pool"] == "recent"
    assert result["picks"][0]["title"] == "Fresh Rec"
    assert result["problems"] == []


def test_recommend_all_time_pool(zotero_env, monkeypatch, paper_factory):
    seen = {}
    monkeypatch.setattr(zotero_client, "seed_ids", lambda limit=10: ["ArXiv:1"])
    monkeypatch.setattr(zotero_client, "known_identities", set)
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: seen.setdefault("pool", pool) and [],
    )
    server.recommend_papers(recent_only=False, interests=["x"])
    assert seen["pool"] == "all-cs"


def test_recommend_excludes_library_papers(
    zotero_env, monkeypatch, paper_factory
):
    monkeypatch.setattr(zotero_client, "seed_ids", lambda limit=10: ["ArXiv:1"])
    monkeypatch.setattr(
        zotero_client, "known_identities", lambda: {"2401.12345"}
    )
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: [
            paper_factory(),  # arxiv_id 2401.12345 — already in library
            paper_factory(title="New One", arxiv_id="2606.1", doi=None),
        ],
    )
    result = server.recommend_papers()
    assert [r["title"] for r in result["picks"]] == ["New One"]


def test_recommend_blends_interests(env, monkeypatch, paper_factory):
    monkeypatch.setattr(
        openalex,
        "search",
        lambda q, max_results: [paper_factory(title=f"About {q}")],
    )
    result = server.recommend_papers(interests=["state space models"])
    assert result["picks"][0]["title"] == "About state space models"


def test_recommend_interleaves_arms(zotero_env, monkeypatch, paper_factory):
    monkeypatch.setattr(zotero_client, "seed_ids", lambda limit=10: ["ArXiv:1"])
    monkeypatch.setattr(zotero_client, "known_identities", set)
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: [
            paper_factory(title=f"Graph {i}", arxiv_id=f"260{i}.1", doi=None)
            for i in range(5)
        ],
    )
    monkeypatch.setattr(
        openalex,
        "search",
        lambda q, max_results: [
            paper_factory(title="Keyword Hit", arxiv_id=None, doi="10.1/kw")
        ],
    )
    result = server.recommend_papers(interests=["topic"], max_results=2)
    # stated interests LEAD; library-graph picks trail, clearly tagged
    assert [r["title"] for r in result["picks"]] == ["Keyword Hit", "Graph 0"]
    assert [r["via"] for r in result["picks"]] == [
        "interest-keyword",
        "related-to-library",
    ]


def test_recommend_filters_malformed_upstream_records(
    zotero_env, monkeypatch, paper_factory
):
    monkeypatch.setattr(zotero_client, "seed_ids", lambda limit=10: ["ArXiv:1"])
    monkeypatch.setattr(zotero_client, "known_identities", set)
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: [
            # truncated junk: no ids, garbage title (a real S2 record)
            paper_factory(title="UvA-DARE (", arxiv_id=None, doi=None),
            paper_factory(title="A Real Paper", arxiv_id="2606.5", doi=None),
        ],
    )
    result = server.recommend_papers()
    assert [r["title"] for r in result["picks"]] == ["A Real Paper"]


def test_recommend_tags_explicit_seed_graph_picks(
    env, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: [
            paper_factory(title="Neighbor", arxiv_id="2606.7", doi=None)
        ],
    )
    result = server.recommend_papers(seed_refs=["2401.12345"])
    assert result["picks"][0]["via"] == "related-to-seeds"


def test_search_filters_malformed_records(env, monkeypatch, paper_factory):
    monkeypatch.setattr(
        openalex,
        "search",
        lambda q, max_results: [
            paper_factory(title="(untitled)", arxiv_id=None, doi=None),
            paper_factory(title="Kept Result"),
        ],
    )
    results = server.search_papers("q")
    assert [r["title"] for r in results] == ["Kept Result"]


def test_recommend_explicit_seeds(env, monkeypatch, paper_factory):
    seen = {}
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: seen.setdefault("seeds", ids) and [],
    )
    server.recommend_papers(seed_refs=["2401.12345"], interests=["x"])
    assert seen["seeds"] == ["ArXiv:2401.12345"]


def test_recommend_backend_down_reports(env, monkeypatch, paper_factory):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())

    def boom(ids, pool, limit):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(server.s2, "recommend", boom)
    result = server.recommend_papers(seed_refs=["2401.12345"])
    assert result["picks"] == []
    assert any("citation-graph arm" in p for p in result["problems"])


def test_recommend_surfaces_arm_failure_alongside_picks(
    env, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        server.s2,
        "recommend",
        lambda ids, pool, limit: [
            paper_factory(title="Graph Pick", arxiv_id="2606.9", doi=None)
        ],
    )

    def keyword_down(q, max_results):
        raise httpx.ConnectError("429")

    monkeypatch.setattr(openalex, "search", keyword_down)
    result = server.recommend_papers(
        seed_refs=["2401.12345"], interests=["coral reef ecology"]
    )
    # picks exist AND the failed interest is reported, never silent
    assert result["picks"][0]["title"] == "Graph Pick"
    assert any("coral reef ecology" in p for p in result["problems"])


def test_recommend_unresolvable_seed_error_carries_diagnosis(env, monkeypatch):
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda ref: (_ for _ in ()).throw(
            ValueError(f"could not resolve: {ref}")
        ),
    )
    with pytest.raises(RuntimeError, match="could not resolve: zzqx"):
        server.recommend_papers(seed_refs=["zzqx"])


def test_recommend_empty_queue_message(zotero_env, monkeypatch):
    monkeypatch.setattr(zotero_client, "seed_ids", lambda limit=10: [])
    with pytest.raises(RuntimeError, match="Reading Queue has no papers"):
        server.recommend_papers()


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
        zotero_client,
        "add_paper",
        lambda p, collections=None: (added.append(p), "K", True)[1:],
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
    assert receipt == "Nothing was sent. | could not resolve: gibberish"


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
    # A deterministic failure (every URL dead/junk) is not retried.
    assert "no usable open-access PDF (won't auto-retry): Dead Link" in receipt


def test_download_failure_notes_queued_retry_with_zotero(
    zotero_env, monkeypatch, paper_factory, sent_documents
):
    good = paper_factory()
    dead = paper_factory(
        title="Dead Link",
        arxiv_id="9999.9",
        doi=None,
        pdf_url="https://mirror.invalid/x.pdf",
    )
    papers = iter([good, dead])
    monkeypatch.setattr(resolver, "resolve", lambda ref: next(papers))

    def fake_download(paper):
        if paper.title == "Dead Link":
            raise httpx.ConnectTimeout("mirror timed out")
        return b"%PDF"

    monkeypatch.setattr(resolver, "download_pdf", fake_download)
    receipt = server.send_papers(["2401.12345", "9999.9"])
    assert "download failed: Dead Link" in receipt
    assert "queued unsent for retry" in receipt


def test_send_papers_records_in_zotero(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    added, marked = [], []

    def fake_add(paper, collections=None):
        added.append(paper)
        return "KEY", "created"

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


def test_send_papers_dry_run_mixed_sizes(env, monkeypatch, paper_factory):
    papers = [
        paper_factory(title="Known", arxiv_id="2401.1"),
        paper_factory(title="Unknown", arxiv_id="2401.2"),
    ]
    monkeypatch.setattr(resolver, "resolve", lambda ref: papers.pop(0))
    monkeypatch.setattr(
        resolver,
        "probe_pdf_size",
        lambda p: 3_000_000 if p.title == "Known" else None,
    )
    receipt = server.send_papers(["2401.1", "2401.2"], dry_run=True)
    # Sum reflects only the sized paper; unknowns are flagged, not summed.
    assert "~3.0 MB for the ones I could size" in receipt
    assert "1 of unknown size" in receipt
    assert "50 MB email limit" in receipt


def test_send_papers_dry_run_unknown_size(env, monkeypatch, paper_factory):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(resolver, "probe_pdf_size", lambda p: None)
    receipt = server.send_papers(["2401.12345"], dry_run=True)
    assert "size unknown" in receipt
    # With no sizes known, the headline must NOT lead with a ~0.0 MB
    # total that undercounts.
    assert "~0.0 MB" not in receipt
    assert "1 of unknown size" in receipt


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
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "created"),
    )
    receipt = server.queue_papers(["2401.12345"])
    assert "Queued 1 new paper(s) in 'Reading Queue'" in receipt


def test_queue_papers_reports_existing(
    env, zotero_ok, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "already_queued"),
    )
    receipt = server.queue_papers(["2401.12345"])
    # No brand-new items, so the lead does not falsely claim a queue add.
    assert "Nothing new to queue" in receipt
    assert "already in queue: A Test Paper" in receipt


def test_queue_papers_reports_requeued_honestly(
    env, zotero_ok, monkeypatch, paper_factory
):
    # A paper filed in a collection but not in the queue, re-added: the
    # receipt must NOT claim "already in queue" (the round-4 bug), and
    # must not lead with a "Queued 0" that reads as nothing happening.
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "requeued"),
    )
    receipt = server.queue_papers(["2401.12345"])
    assert "already in queue" not in receipt
    assert "Queued 0" not in receipt
    assert receipt.startswith("Re-added 1 paper(s)")
    assert "already in your library): A Test Paper" in receipt


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
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "created"),
    )
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    receipt = server.queue_papers(["10.1/x"])
    assert tagged == ["KEY"]
    assert "no open-access PDF (won't be auto-sent)" in receipt


def test_queue_papers_biorxiv_not_flagged_no_pdf(
    env, zotero_ok, monkeypatch, paper_factory
):
    # A bioRxiv DOI whose OA index is blank still gets a PDF URL from
    # the model, so it must NOT be tagged no-OA (round-7 false negative).
    tagged = []
    biorxiv = paper_factory(
        pdf_url=None, arxiv_id=None, doi="10.1101/2021.06.11.448104"
    )
    assert biorxiv.pdf_url is not None  # model populated the fallback
    monkeypatch.setattr(resolver, "resolve", lambda ref: biorxiv)
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "created"),
    )
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    receipt = server.queue_papers(["10.1101/2021.06.11.448104"])
    assert tagged == []
    assert "no open-access PDF" not in receipt


def test_queue_papers_fails_fast_without_zotero(env, monkeypatch):
    def explode(ref):
        raise AssertionError("resolver must not be called")

    monkeypatch.setattr(resolver, "resolve", explode)
    with pytest.raises(RuntimeError, match="paperboy setup"):
        server.queue_papers(["2401.12345"])


# --- list / remove ---------------------------------------------------------


def test_list_collections_tool(env, zotero_ok, monkeypatch):
    entries = [{"name": "ML", "items": 3, "parent": None}]
    monkeypatch.setattr(zotero_client, "list_collections", lambda: entries)
    assert server.list_collections() == entries


def test_file_papers_tool(env, zotero_ok, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "file_by_refs",
        lambda refs, name: (["Paper A"], ["nope"], []),
    )
    receipt = server.file_papers(["10.1/a", "nope"], "Bayesian Methods")
    assert "Filed 1 item(s) under 'Bayesian Methods'" in receipt
    assert "Paper A" in receipt
    assert "Not found in queue: nope" in receipt
    # unresolved refs get a next-step, not just an error
    assert "queue_papers" in receipt


def test_file_papers_nothing_matched_does_not_imply_collection(
    env, zotero_ok, monkeypatch
):
    # A phantom-name file with no matches must not read as if the
    # collection now exists ("Filed 0 ... under 'X'").
    monkeypatch.setattr(
        zotero_client, "file_by_refs", lambda refs, name: ([], ["nope"], [])
    )
    receipt = server.file_papers(["nope"], "Phantom Topic")
    assert "Filed 0" not in receipt
    assert "'Phantom Topic' was not created" in receipt
    assert "queue_papers" in receipt


def test_queue_papers_files_into_collections(
    env, zotero_ok, monkeypatch, paper_factory
):
    seen = {}

    def fake_add(paper, collections=None):
        seen["collections"] = collections
        return "KEY", "created"

    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", fake_add)
    receipt = server.queue_papers(
        ["2401.12345"], collections=["State Space Models"]
    )
    assert seen["collections"] == ["State Space Models"]
    assert "filed under: State Space Models" in receipt


def test_queue_papers_reports_filing_for_already_queued(
    env, zotero_ok, monkeypatch, paper_factory
):
    # Filing happens even when the paper was already queued; a receipt
    # reading as a pure no-op would misreport a real mutation.
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("KEY", "already_queued"),
    )
    receipt = server.queue_papers(["2401.12345"], collections=["Topic"])
    assert "already in queue" in receipt
    assert "also filed under: Topic" in receipt


def test_queue_papers_drops_reading_queue_from_collections(
    env, zotero_ok, monkeypatch, paper_factory
):
    seen = {}

    def fake_add(paper, collections=None):
        seen["collections"] = collections
        return "KEY", "created"

    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", fake_add)
    receipt = server.queue_papers(["2401.12345"], collections=["Reading Queue"])
    assert seen["collections"] is None
    assert "dropped the Reading Queue from collections" in receipt


def test_resolve_all_translates_item_keys(
    zotero_env, monkeypatch, paper_factory
):
    # A Zotero item key from list_queue must work in send/queue too:
    # it is swapped for the item's own scholarly id before resolution.
    monkeypatch.setattr(
        zotero_client,
        "scholarly_ref_for_key",
        lambda ref: "2401.12345" if ref == "S83KSPCA" else None,
    )
    seen = {}

    def fake_resolve(ref):
        seen["ref"] = ref
        return paper_factory()

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    resolved, problems = server._resolve_all(["S83KSPCA"])
    assert seen["ref"] == "2401.12345"
    assert len(resolved) == 1 and problems == []


def test_resolve_all_reports_429_as_transient(env, monkeypatch):
    request = httpx.Request("GET", "https://api.openalex.org/works")

    def throttled(ref):
        raise httpx.HTTPStatusError(
            "429",
            request=request,
            response=httpx.Response(429, request=request),
        )

    monkeypatch.setattr(resolver, "resolve", throttled)
    resolved, problems = server._resolve_all(["Mastering the game of Go"])
    assert resolved == []
    assert "rate-limited" in problems[0]
    assert "retry after the rate limit lifts" in problems[0]
    assert "could not resolve" not in problems[0]


def test_resolve_all_keeps_permanent_4xx_as_could_not_resolve(env, monkeypatch):
    request = httpx.Request("GET", "https://api.crossref.org/works/x")

    def gone(ref):
        raise httpx.HTTPStatusError(
            "404",
            request=request,
            response=httpx.Response(404, request=request),
        )

    monkeypatch.setattr(resolver, "resolve", gone)
    _, problems = server._resolve_all(["10.9999/nope"])
    assert "could not resolve" in problems[0]


def test_send_papers_files_into_collections(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    seen = {}

    def fake_add(paper, collections=None):
        seen["collections"] = collections
        return "KEY", "created"

    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", fake_add)
    receipt = server.send_papers(["2401.12345"], collections=["ML"])
    assert seen["collections"] == ["ML"]
    assert "filed under: ML" in receipt


def test_remove_reports_kept_in_library(env, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "remove_by_refs",
        lambda refs: (
            [
                {
                    "title": "Filed One",
                    "was_sent": True,
                    "kept_in_library": True,
                }
            ],
            [],
            [],
        ),
    )
    receipt = server.remove_from_queue(["10.1/a"])
    assert "kept in the library" in receipt
    assert "sent-state preserved" in receipt
    # kept items keep their sent tag, so no re-delivery warning
    assert "WILL re-deliver" not in receipt
    # each title reported once, not listed then re-annotated
    assert receipt.count("Filed One") == 1
    assert "Trash" not in receipt  # a kept item is not trashed


def test_send_papers_files_already_sent_papers(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    filed = []
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client, "find_item", lambda p: {"key": "K", "data": {}}
    )
    monkeypatch.setattr(zotero_client, "is_sent", lambda item: True)
    monkeypatch.setattr(
        zotero_client,
        "file_item",
        lambda item, collections: filed.append(collections),
    )
    receipt = server.send_papers(["2401.12345"], collections=["ML"])
    assert sent_documents == []  # still not re-sent
    assert filed == [["ML"]]  # but the filing happened
    assert "filed into requested collections" in receipt


def test_send_papers_dry_run_does_not_file(
    zotero_env, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client, "find_item", lambda p: {"key": "K", "data": {}}
    )
    monkeypatch.setattr(zotero_client, "is_sent", lambda item: True)

    def explode(item, collections):
        raise AssertionError("dry_run must not file")

    monkeypatch.setattr(zotero_client, "file_item", explode)
    server.send_papers(["2401.12345"], collections=["ML"], dry_run=True)


def test_collections_ignored_note_without_zotero(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    receipt = server.send_papers(["2401.12345"], collections=["ML"])
    assert "collections ignored (Zotero is not configured)" in receipt


def test_empty_collection_names_are_dropped(
    env, zotero_ok, monkeypatch, paper_factory
):
    seen = {}

    def fake_add(paper, collections=None):
        seen["collections"] = collections
        return "KEY", "created"

    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(zotero_client, "add_paper", fake_add)
    receipt = server.queue_papers(["2401.12345"], collections=["", "  "])
    assert seen["collections"] is None
    assert "ignored empty collection name(s)" in receipt


def test_file_papers_rejects_empty_name(env, zotero_ok):
    receipt = server.file_papers(["10.1/a"], "   ")
    assert "must be non-empty" in receipt


def test_unfile_papers_tool(env, zotero_ok, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "unfile_by_refs",
        lambda refs, name: (["Paper A"], ["nope"], []),
    )
    receipt = server.unfile_papers(["10.1/a", "nope"], "Bayesian Methods")
    assert "Removed 1 item(s) from 'Bayesian Methods'" in receipt
    assert "Paper A" in receipt
    assert "Not found in that collection: nope" in receipt


def test_file_papers_refuses_reading_queue_target(env, zotero_ok):
    receipt = server.file_papers(["10.1/a"], "Reading Queue")
    assert "Nothing filed" in receipt
    assert "queue_papers" in receipt


def test_unfile_papers_refuses_reading_queue_target(env, zotero_ok):
    receipt = server.unfile_papers(["10.1/a"], "reading queue")
    assert "Nothing removed" in receipt
    assert "remove_from_queue" in receipt


def test_file_papers_reports_ambiguity(env, zotero_ok, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "file_by_refs",
        lambda refs, name: (
            [],
            [],
            [
                {
                    "ref": "dup title",
                    "candidates": [
                        {"key": "KEY1", "id": "arXiv:1", "added": ""},
                        {"key": "KEY2", "id": "arXiv:1", "added": ""},
                    ],
                }
            ],
        ),
    )
    receipt = server.file_papers(["dup title"], "Topic")
    assert "NOT filed (ambiguous" in receipt
    assert "item key KEY1" in receipt and "item key KEY2" in receipt


def test_unfile_papers_reports_ambiguity(env, zotero_ok, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "unfile_by_refs",
        lambda refs, name: (
            [],
            [],
            [
                {
                    "ref": "dup title",
                    "candidates": [
                        {"key": "KEY1", "id": "no other id", "added": ""},
                        {"key": "KEY2", "id": "no other id", "added": ""},
                    ],
                }
            ],
        ),
    )
    receipt = server.unfile_papers(["dup title"], "Topic")
    assert "NOT removed (ambiguous" in receipt
    assert "item key KEY1" in receipt


def test_remove_from_queue_ignores_blank_refs(env, monkeypatch):
    seen = {}

    def fake_remove(refs):
        seen["refs"] = refs
        return [], [], []

    monkeypatch.setattr(zotero_client, "remove_by_refs", fake_remove)
    receipt = server.remove_from_queue(["", "  ", "10.1/a"])
    assert seen["refs"] == ["10.1/a"]
    assert "ignored empty ref(s)" in receipt


def test_unfile_papers_unknown_collection(env, zotero_ok, monkeypatch):
    def raise_missing(refs, name):
        raise ValueError(f"No collection named {name!r}.")

    monkeypatch.setattr(zotero_client, "unfile_by_refs", raise_missing)
    receipt = server.unfile_papers(["10.1/a"], "Ghost")
    assert "Nothing removed" in receipt
    assert "Ghost" in receipt


def test_unfile_papers_rejects_empty_name(env, zotero_ok):
    receipt = server.unfile_papers(["10.1/a"], "  ")
    assert "must be non-empty" in receipt


def test_list_queue_tool(env, monkeypatch):
    entries = [{"title": "T", "ref": "10.1/x", "status": "unsent", "added": ""}]
    monkeypatch.setattr(zotero_client, "list_queue", lambda: entries)
    assert server.list_queue() == entries


def test_remove_from_queue_tool(env, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "remove_by_refs",
        lambda refs: (
            [
                {
                    "title": "Paper A",
                    "was_sent": False,
                    "kept_in_library": False,
                }
            ],
            ["nope"],
            [],
        ),
    )
    receipt = server.remove_from_queue(["10.1/a", "nope"])
    assert "Removed 1 item(s)" in receipt and "Paper A" in receipt
    assert "Not found in queue: nope" in receipt
    assert "re-deliver" not in receipt


def test_remove_from_queue_warns_on_sent_items(env, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "remove_by_refs",
        lambda refs: (
            [
                {
                    "title": "Read One",
                    "was_sent": True,
                    "kept_in_library": False,
                }
            ],
            [],
            [],
        ),
    )
    receipt = server.remove_from_queue(["10.1/a"])
    assert "Zotero's Trash" in receipt
    assert "WILL re-deliver: Read One" in receipt


def test_remove_from_queue_reports_ambiguity(env, monkeypatch):
    monkeypatch.setattr(
        zotero_client,
        "remove_by_refs",
        lambda refs: (
            [],
            [],
            [
                {
                    "ref": "same title",
                    "candidates": [
                        {
                            "key": "PRE",
                            "id": "arXiv:2401.12345",
                            "added": "2026-07-01",
                        },
                        {
                            "key": "PUB",
                            "id": "10.1000/pub",
                            "added": "2026-07-10",
                        },
                    ],
                }
            ],
        ),
    )
    receipt = server.remove_from_queue(["same title"])
    assert "Removed 0 item(s)" in receipt
    assert "NOT removed (ambiguous" in receipt
    assert "item key PRE (arXiv:2401.12345, added 2026-07-01)" in receipt
    assert "re-run with that item key" in receipt


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


def test_send_queue_transient_download_failure_is_not_tagged(
    env, monkeypatch, paper_factory, sent_documents
):
    # A network blip must not permanently tag a paper no-oa-pdf and
    # drop it from auto-send — it stays unsent for the next run.
    items = [{"key": "BLIP", "data": {"url": "https://arxiv.org/abs/1.1"}}]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    tagged = []
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        resolver,
        "download_pdf",
        lambda paper: (_ for _ in ()).throw(httpx.ConnectTimeout("blip")),
    )
    receipt = server.send_queue()
    assert tagged == []
    assert "left unsent" in receipt and "will retry" in receipt


def test_send_queue_junk_pdf_is_tagged(
    env, monkeypatch, paper_factory, sent_documents
):
    # Deterministic junk (every URL served non-PDF content) IS tagged,
    # so send_queue doesn't retry it forever.
    items = [{"key": "JUNK", "data": {"url": "https://arxiv.org/abs/1.2"}}]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    tagged = []
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        resolver,
        "download_pdf",
        lambda paper: (_ for _ in ()).throw(ValueError("non-PDF content")),
    )
    receipt = server.send_queue()
    assert tagged == ["JUNK"]
    assert "won't retry" in receipt


def test_download_pdf_reraises_transient_transport_errors(
    monkeypatch, paper_factory
):
    monkeypatch.setattr(
        resolver,
        "client",
        httpx.Client(
            transport=httpx.MockTransport(
                lambda r: (_ for _ in ()).throw(
                    httpx.ConnectTimeout("boom", request=r)
                )
            )
        ),
    )
    with pytest.raises(httpx.ConnectTimeout):
        resolver.download_pdf(paper_factory())


def test_resolve_all_zotero_outage_is_transient(
    zotero_env, monkeypatch, paper_factory
):
    def unavailable(ref):
        raise zotero_client.ZoteroUnavailableError(
            "Zotero is temporarily unreachable while looking up item "
            "key ABCD1234 — retry"
        )

    monkeypatch.setattr(zotero_client, "scholarly_ref_for_key", unavailable)
    resolved, problems = server._resolve_all(["ABCD1234"])
    assert resolved == []
    assert "temporarily unreachable" in problems[0]
    assert "could not resolve" not in problems[0].lower()


def test_send_papers_drops_blank_refs(env, monkeypatch, paper_factory):
    seen = []

    def fake_resolve(ref):
        seen.append(ref)
        return paper_factory(pdf_url=None)

    monkeypatch.setattr(resolver, "resolve", fake_resolve)
    receipt = server.send_papers(["  ", "2401.12345"], dry_run=True)
    assert seen == ["2401.12345"]
    assert "ignored empty ref(s)" in receipt


def test_dry_run_mentions_pending_filing(
    zotero_env, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(resolver, "probe_pdf_size", lambda paper: 2_000_000)
    receipt = server.send_papers(
        ["2401.12345"], dry_run=True, collections=["Topic"]
    )
    assert "a real send would also file under: Topic" in receipt


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


def test_http_auth_requires_token(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(SystemExit, match="MCP_AUTH_TOKEN"):
        server._http_auth()


def test_http_auth_rejects_short_token(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "short")
    with pytest.raises(SystemExit):
        server._http_auth()


def test_http_auth_accepts_long_token(monkeypatch):
    token = "x" * 43
    monkeypatch.setenv("MCP_AUTH_TOKEN", token)
    for var in (
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
        "SERVER_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    verifier = server._http_auth()
    assert token in verifier.tokens


def test_http_auth_uses_oauth_when_configured(monkeypatch):
    from paperboy import oauth

    token = "x" * 43
    monkeypatch.setenv("MCP_AUTH_TOKEN", token)
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "id.apps")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "GOCSPX-x")
    monkeypatch.setenv("SERVER_BASE_URL", "https://paperboy.example.com")
    monkeypatch.setenv("OAUTH_ALLOWED_EMAILS", "owner@example.com")
    auth = server._http_auth()
    assert isinstance(auth, oauth.PaperboyAuth)


def test_all_failed_junk_downloads_are_tagged(
    zotero_env, monkeypatch, paper_factory, sent_documents
):
    # The all-failed branch must tag junk like the delivered branch
    # does, or the "won't auto-retry" receipt is false.
    tagged = []
    monkeypatch.setattr(zotero_client, "mark_no_pdf", tagged.append)
    monkeypatch.setattr(
        zotero_client,
        "add_paper",
        lambda p, collections=None: ("JUNKKEY", "created"),
    )
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        resolver,
        "download_pdf",
        lambda paper: (_ for _ in ()).throw(ValueError("non-PDF content")),
    )
    receipt = server.send_papers(["2401.12345"])
    assert "won't auto-retry" in receipt
    assert tagged == ["JUNKKEY"]
    assert sent_documents == []


def test_download_retry_note_honest_without_zotero(
    env, monkeypatch, paper_factory, sent_documents
):
    # Without Zotero there is no queue: the receipt must not claim
    # "queued unsent for retry".
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        resolver,
        "download_pdf",
        lambda paper: (_ for _ in ()).throw(httpx.ConnectError("reset")),
    )
    receipt = server.send_papers(["2401.12345"])
    assert "queued unsent for retry" not in receipt
    assert "Zotero is not configured" in receipt


def test_search_429_advice_matches_source(env, monkeypatch):
    request = httpx.Request("GET", "https://export.arxiv.org/api/query")

    def throttled(q, max_results):
        raise httpx.HTTPStatusError(
            "429",
            request=request,
            response=httpx.Response(429, request=request),
        )

    monkeypatch.setattr(arxiv, "search", throttled)
    with pytest.raises(RuntimeError) as excinfo:
        server.search_papers("q", source="arxiv")
    assert "use source='arxiv'" not in str(excinfo.value)


def test_send_papers_receipt_survives_zotero_outage_after_delivery(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    # Delivery already happened; a Zotero outage during bookkeeping
    # must not destroy the receipt (a lost receipt invites a re-send).
    def zotero_down(paper, collections=None):
        raise httpx.ConnectError("zotero.org unreachable")

    monkeypatch.setattr(zotero_client, "add_paper", zotero_down)
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    receipt = server.send_papers(["2401.12345"])
    assert len(sent_documents) == 1
    assert "Sent 1 document(s)" in receipt
    assert "WARNING: delivered, but recording in Zotero failed" in receipt
    assert "do NOT re-send" in receipt


def test_send_queue_receipt_survives_zotero_outage_after_delivery(
    env, monkeypatch, paper_factory, sent_documents, no_download
):
    items = [{"key": "GOOD", "data": {"url": "https://arxiv.org/abs/1.1"}}]
    monkeypatch.setattr(zotero_client, "unsent_queue_items", lambda: items)
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())

    def tag_down(key):
        raise httpx.ConnectError("zotero.org unreachable")

    monkeypatch.setattr(zotero_client, "mark_sent", tag_down)
    receipt = server.send_queue()
    assert len(sent_documents) == 1
    assert "Sent 1 document(s)" in receipt
    assert "WARNING: delivered, but tagging sent in Zotero failed" in receipt


# --- add_to_library (track-only) ---------------------------------------


def test_add_to_library_catalogs_without_queue(
    zotero_env, monkeypatch, paper_factory
):
    calls = []
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client,
        "catalog_paper",
        lambda p, collections=None: (
            calls.append(collections) or ("K", "created", ["Theory"])
        ),
    )
    result = server.add_to_library(["Some Paper Title"], collections=["Theory"])
    assert "Added 1 to your library" in result
    assert "filed under: Theory" in result
    assert calls == [["Theory"]]


def test_add_to_library_reports_existing_membership(
    zotero_env, monkeypatch, paper_factory
):
    monkeypatch.setattr(resolver, "resolve", lambda ref: paper_factory())
    monkeypatch.setattr(
        zotero_client,
        "catalog_paper",
        lambda p, collections=None: (
            "K",
            "existing",
            ["Theory", "Reading Queue"],
        ),
    )
    result = server.add_to_library(["Some Paper Title"])
    assert "already in your library" in result
    assert "currently in: Theory; Reading Queue" in result


def test_add_to_library_no_valid_refs(zotero_env, monkeypatch):
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda ref: (_ for _ in ()).throw(ValueError("nope")),
    )
    result = server.add_to_library(["bad ref"])
    assert result.startswith("Nothing was added")


# --- add_book ----------------------------------------------------------


def test_add_book_catalogs_book(zotero_env, monkeypatch):
    from paperboy import books
    from paperboy.books import Book

    monkeypatch.setattr(
        books,
        "resolve_book",
        lambda ident: Book(
            title="Lebesgue Measure", authors=["Nelson"], isbn="9781470421991"
        ),
    )
    monkeypatch.setattr(
        zotero_client,
        "add_book",
        lambda b, collections=None: ("K", "created", ["Textbooks"]),
    )
    result = server.add_book("978-1-4704-2199-1", collections=["Textbooks"])
    assert "Catalogued book" in result
    assert "Lebesgue Measure" in result
    assert "9781470421991" in result


def test_add_book_resolution_failure(zotero_env, monkeypatch):
    from paperboy import books

    monkeypatch.setattr(
        books,
        "resolve_book",
        lambda ident: (_ for _ in ()).throw(ValueError("Closest was 'X' by Y")),
    )
    result = server.add_book("some fuzzy title")
    assert "Could not add book" in result
    assert "Closest was" in result


# --- attach_pdf --------------------------------------------------------


def test_attach_pdf_downloads_and_attaches(zotero_env, monkeypatch):
    import os

    captured = {}

    def fake_attach(**kwargs):
        captured.update(kwargs)
        captured["path_exists"] = os.path.exists(kwargs["pdf_path"])
        return ("K", "created", True)

    monkeypatch.setattr(resolver, "fetch_pdf", lambda url: b"%PDF-1.7 data")
    monkeypatch.setattr(zotero_client, "attach_pdf_item", fake_attach)
    result = server.attach_pdf(
        "https://host/paper.pdf",
        "A Grey Lit Preprint",
        ["Author One"],
        item_type="report",
        collections=["Grey Lit"],
    )
    assert "Added report 'A Grey Lit Preprint'" in result
    assert "PDF attached" in result
    assert captured["item_type"] == "report"
    assert captured["path_exists"] is True  # temp file present during attach


def test_attach_pdf_rejects_unknown_item_type(zotero_env):
    result = server.attach_pdf("http://x/y.pdf", "T", [], item_type="banana")
    assert "Unsupported item_type" in result


def test_attach_pdf_local_path_is_a_directory(zotero_env, tmp_path):
    # A local path that is a directory (e.g. a dragged folder) must return
    # a clean receipt, not raise an uncaught IsADirectoryError.
    result = server.attach_pdf(str(tmp_path), "T", ["A"])
    assert "Could not read" in result


def test_attach_pdf_non_pdf_content(zotero_env, monkeypatch):
    monkeypatch.setattr(
        resolver,
        "fetch_pdf",
        lambda url: (_ for _ in ()).throw(
            ValueError("returned non-PDF content")
        ),
    )
    result = server.attach_pdf("https://host/notreally.pdf", "T", [])
    assert "Could not ingest the PDF" in result


def test_attach_pdf_send_delivers(zotero_env, monkeypatch, sent_documents):
    monkeypatch.setattr(resolver, "fetch_pdf", lambda url: b"%PDF- bytes")
    monkeypatch.setattr(
        zotero_client, "attach_pdf_item", lambda **kw: ("K", "created", True)
    )
    result = server.attach_pdf(
        "https://host/oa-textbook.pdf",
        "Open Textbook",
        ["Author"],
        item_type="book",
        send=True,
    )
    assert "sent to your e-reader" in result
    assert len(sent_documents) == 1


# --- review-round fixes -------------------------------------------------


def test_attach_pdf_invalid_url_gets_clean_receipt(zotero_env, monkeypatch):
    def raise_invalid(url):
        raise httpx.InvalidURL("Invalid port: ':1'")

    monkeypatch.setattr(resolver, "fetch_pdf", raise_invalid)
    result = server.attach_pdf("http://[::1/paper.pdf", "T", ["A"])
    assert result.startswith("Not a valid URL")


def test_attach_pdf_reuse_receipt_says_found_existing(zotero_env, monkeypatch):
    monkeypatch.setattr(resolver, "fetch_pdf", lambda url: b"%PDF-1.7")
    monkeypatch.setattr(
        zotero_client,
        "attach_pdf_item",
        lambda **kw: ("K", "existing", True),
    )
    result = server.attach_pdf("https://x/y.pdf", "Notes", ["A"])
    assert "Found existing" in result
    assert "PDF attached" in result


def test_attach_pdf_failed_upload_receipt_promises_safe_retry(
    zotero_env, monkeypatch
):
    monkeypatch.setattr(resolver, "fetch_pdf", lambda url: b"%PDF-1.7")
    monkeypatch.setattr(
        zotero_client,
        "attach_pdf_item",
        lambda **kw: ("K", "created", False),
    )
    result = server.attach_pdf("https://x/y.pdf", "Notes", ["A"])
    assert "did not upload" in result
    assert "not duplicated" in result


def test_send_papers_files_aliased_already_sent_item_once(
    zotero_env, monkeypatch, paper_factory, sent_documents
):
    # One Zotero item can be named by two refs (its DOI and its arXiv
    # id). Filing it twice would 412 on the stale second write; it must
    # be filed once and the send must survive a filing failure.
    doi_form = paper_factory(
        title="Published Form", arxiv_id=None, doi="10.1/x", pdf_url=None
    )
    arxiv_form = paper_factory(title="Preprint Form")
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda ref: doi_form if ref == "10.1/x" else arxiv_form,
    )
    shared_item = {"key": "S1", "data": {"collections": [], "tags": []}}
    monkeypatch.setattr(zotero_client, "find_item", lambda p: shared_item)
    monkeypatch.setattr(zotero_client, "is_sent", lambda item: True)
    filed = []
    monkeypatch.setattr(
        zotero_client,
        "file_item",
        lambda item, collections: filed.append(item["key"]),
    )
    result = server.send_papers(["10.1/x", "2401.12345"], collections=["ML"])
    assert filed == ["S1"]  # once, not twice
    assert "already sent" in result


def test_send_papers_survives_filing_failure(
    zotero_env, monkeypatch, paper_factory, sent_documents, no_download
):
    # A filing hiccup on an already-sent item must not kill the send of
    # the batch's fresh papers.
    sent_paper = paper_factory(title="Old One", arxiv_id=None, doi="10.1/old")
    fresh_paper = paper_factory(title="Fresh One")
    monkeypatch.setattr(
        resolver,
        "resolve",
        lambda ref: sent_paper if ref == "10.1/old" else fresh_paper,
    )
    sent_item = {
        "key": "S1",
        "data": {"title": "Old One", "collections": [], "tags": []},
    }
    monkeypatch.setattr(
        zotero_client,
        "find_item",
        lambda p: sent_item if p is sent_paper else None,
    )
    monkeypatch.setattr(
        zotero_client, "is_sent", lambda item: item is sent_item
    )

    def failing_file(item, collections):
        raise RuntimeError("412 stale")

    monkeypatch.setattr(zotero_client, "file_item", failing_file)
    result = server.send_papers(["10.1/old", "2401.12345"], collections=["ML"])
    assert len(sent_documents) == 1  # the fresh paper still shipped
    assert "could not file already-sent 'Old One'" in result
    assert "the send itself continues" in result
    # The receipt must not simultaneously claim the filing happened.
    assert "filed into requested collections" not in result


def test_send_queue_untitled_idless_item_skips_locally(zotero_env, monkeypatch):
    # An untitled item with no DOI/URL must be skipped locally with the
    # honest message — its item key must never be sent to a network
    # title search as if it were a title.
    monkeypatch.setattr(
        zotero_client,
        "unsent_queue_items",
        lambda: [{"key": "KEY1AAAA", "data": {"title": "", "collections": []}}],
    )

    def never(ref):
        raise AssertionError(f"resolver must not be called with {ref!r}")

    monkeypatch.setattr(resolver, "resolve", never)
    result = server.send_queue()
    assert "KEY1AAAA (no DOI, URL, or title)" in result
