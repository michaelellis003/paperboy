import pytest

from paperboy import zotero_client


class FakeZotero:
    def __init__(self, items=None, collections=None):
        self.items = items or []
        self.existing_collections = collections or [
            {"key": "COLL", "data": {"name": "Reading Queue"}}
        ]
        self.created = []
        self.deleted = []
        self.tagged = []
        self.fail_create = False

    def everything(self, value):
        return value

    def collections(self):
        return self.existing_collections

    def create_collections(self, payload):
        return {"successful": {"0": {"key": "NEWCOLL"}}}

    def collection_items_top(self, key):
        return self.items

    def item_template(self, item_type):
        return {"itemType": item_type}

    def create_items(self, payload):
        if self.fail_create:
            return {"failed": {"0": {"message": "bad"}}, "successful": {}}
        self.created.extend(payload)
        return {"failed": {}, "successful": {"0": {"key": "NEWITEM"}}}

    def item(self, key):
        for item in self.items:
            if item["key"] == key:
                return item
        return {"key": key, "data": {}}

    def add_tags(self, item, tag):
        self.tagged.append((item["key"], tag))

    def delete_item(self, item):
        self.deleted.append(item["key"])
        self.items = [i for i in self.items if i["key"] != item["key"]]


@pytest.fixture
def fake_api(env, monkeypatch):
    fake = FakeZotero()
    monkeypatch.setattr(zotero_client, "_api", lambda: fake)
    return fake


def test_add_arxiv_paper_creates_preprint(fake_api, paper_factory):
    key, created = zotero_client.add_paper(paper_factory())
    assert (key, created) == ("NEWITEM", True)
    item = fake_api.created[0]
    assert item["itemType"] == "preprint"
    assert item["archiveID"] == "arXiv:2401.12345"
    assert item["collections"] == ["COLL"]


def test_add_doi_paper_creates_journal_article(fake_api, paper_factory):
    paper = paper_factory(arxiv_id=None, doi="10.1038/nature12373")
    zotero_client.add_paper(paper)
    item = fake_api.created[0]
    assert item["itemType"] == "journalArticle"
    assert item["DOI"] == "10.1038/nature12373"


def test_add_paper_dedups_on_arxiv_url(fake_api, paper_factory):
    fake_api.items = [
        {
            "key": "EXISTING",
            "data": {"url": "https://arxiv.org/abs/2401.12345"},
        }
    ]
    assert zotero_client.add_paper(paper_factory()) == ("EXISTING", False)
    assert fake_api.created == []


def test_add_paper_dedups_on_doi_case_insensitive(fake_api, paper_factory):
    fake_api.items = [
        {"key": "EXISTING", "data": {"DOI": "10.1038/NATURE12373"}}
    ]
    paper = paper_factory(arxiv_id=None, doi="10.1038/nature12373")
    assert zotero_client.add_paper(paper) == ("EXISTING", False)


def test_add_paper_rejected_by_zotero(fake_api, paper_factory):
    fake_api.fail_create = True
    with pytest.raises(RuntimeError, match="rejected"):
        zotero_client.add_paper(paper_factory())


def test_matches_on_archive_id(fake_api, paper_factory):
    fake_api.items = [
        {
            "key": "K1",
            "data": {
                "url": "https://doi.org/10.65215/junk",
                "archiveID": "arXiv:2401.12345",
            },
        }
    ]
    item = zotero_client.find_item(paper_factory())
    assert item is not None and item["key"] == "K1"


def test_find_item_and_is_sent(fake_api, paper_factory):
    fake_api.items = [
        {
            "key": "K1",
            "data": {
                "url": "https://arxiv.org/abs/2401.12345",
                "tags": [{"tag": "sent-to-ereader"}],
            },
        }
    ]
    item = zotero_client.find_item(paper_factory())
    assert item is not None and item["key"] == "K1"
    assert zotero_client.is_sent(item) is True
    assert zotero_client.find_item(paper_factory(arxiv_id="9999.9")) is None


def test_unsent_excludes_sent_and_no_pdf(fake_api):
    fake_api.items = [
        {"key": "A", "data": {"tags": [{"tag": "sent-to-ereader"}]}},
        {"key": "B", "data": {"tags": [{"tag": zotero_client.NO_PDF_TAG}]}},
        {"key": "C", "data": {}},
    ]
    keys = [item["key"] for item in zotero_client.unsent_queue_items()]
    assert keys == ["C"]


def test_mark_sent_is_idempotent(fake_api):
    fake_api.items = [
        {"key": "A", "data": {"tags": [{"tag": "sent-to-ereader"}]}},
        {"key": "B", "data": {"tags": []}},
    ]
    zotero_client.mark_sent("A")
    zotero_client.mark_sent("B")
    assert fake_api.tagged == [("B", "sent-to-ereader")]


def test_mark_no_pdf(fake_api):
    fake_api.items = [{"key": "A", "data": {"tags": []}}]
    zotero_client.mark_no_pdf("A")
    assert fake_api.tagged == [("A", zotero_client.NO_PDF_TAG)]


def test_list_queue_statuses(fake_api):
    fake_api.items = [
        {
            "key": "A",
            "data": {
                "title": "Sent One",
                "DOI": "10.1/a",
                "tags": [{"tag": "sent-to-ereader"}],
                "dateAdded": "2026-07-01T00:00:00Z",
            },
        },
        {
            "key": "B",
            "data": {
                "title": "Stuck One",
                "url": "https://x",
                "tags": [{"tag": zotero_client.NO_PDF_TAG}],
            },
        },
        {"key": "C", "data": {"title": "Fresh One", "url": "https://y"}},
    ]
    entries = zotero_client.list_queue()
    assert [(e["title"], e["status"]) for e in entries] == [
        ("Sent One", "sent"),
        ("Stuck One", "no-open-access-pdf"),
        ("Fresh One", "unsent"),
    ]
    assert entries[0]["added"] == "2026-07-01"


def test_remove_by_refs(fake_api):
    fake_api.items = [
        {"key": "A", "data": {"title": "Alpha", "DOI": "10.1/alpha"}},
        {
            "key": "B",
            "data": {"title": "Beta", "url": "https://arxiv.org/abs/2401.1"},
        },
    ]
    removed, misses = zotero_client.remove_by_refs(
        ["10.1/alpha", "beta", "nonexistent"]
    )
    assert removed == ["Alpha", "Beta"]
    assert misses == ["nonexistent"]
    assert fake_api.deleted == ["A", "B"]


def test_creates_missing_collection(fake_api, paper_factory):
    fake_api.existing_collections = [
        {"key": "OTHER", "data": {"name": "Something Else"}}
    ]
    zotero_client.add_paper(paper_factory())
    assert fake_api.created[0]["collections"] == ["NEWCOLL"]


def test_api_requires_configuration(env, monkeypatch):
    import paperboy.config

    monkeypatch.setattr(paperboy.config, "_settings", None)
    zotero_client._api.cache_clear()
    with pytest.raises(RuntimeError, match="not configured"):
        zotero_client._api()
