import pytest

from paperboy import zotero_client


class FakeZotero:
    def __init__(self, items=None, collections=None):
        self.items = items or []
        self.existing_collections = collections or [
            {"key": "COLL", "data": {"name": "Reading Queue"}}
        ]
        self.created = []
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
        return {"key": key, "data": {}}

    def add_tags(self, item, tag):
        self.tagged.append((item["key"], tag))


@pytest.fixture
def fake_api(env, monkeypatch):
    fake = FakeZotero()
    monkeypatch.setattr(zotero_client, "_api", lambda: fake)
    return fake


def test_add_arxiv_paper_creates_preprint(fake_api, paper_factory):
    key = zotero_client.add_paper(paper_factory())
    assert key == "NEWITEM"
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
    assert zotero_client.add_paper(paper_factory()) == "EXISTING"
    assert fake_api.created == []


def test_add_paper_dedups_on_doi_case_insensitive(fake_api, paper_factory):
    fake_api.items = [
        {"key": "EXISTING", "data": {"DOI": "10.1038/NATURE12373"}}
    ]
    paper = paper_factory(arxiv_id=None, doi="10.1038/nature12373")
    assert zotero_client.add_paper(paper) == "EXISTING"


def test_add_paper_rejected_by_zotero(fake_api, paper_factory):
    fake_api.fail_create = True
    with pytest.raises(RuntimeError, match="rejected"):
        zotero_client.add_paper(paper_factory())


def test_unsent_queue_items_filters_sent_tag(fake_api):
    fake_api.items = [
        {"key": "A", "data": {"tags": [{"tag": "sent-to-ereader"}]}},
        {"key": "B", "data": {"tags": [{"tag": "other"}]}},
        {"key": "C", "data": {}},
    ]
    keys = [item["key"] for item in zotero_client.unsent_queue_items()]
    assert keys == ["B", "C"]


def test_mark_sent_tags_item(fake_api):
    zotero_client.mark_sent("ITEM1")
    assert fake_api.tagged == [("ITEM1", "sent-to-ereader")]


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
