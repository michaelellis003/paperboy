import pytest

from paperboy import zotero_client
from paperboy.books import Book


class FakeZotero:
    def __init__(self, items=None, collections=None):
        self.queue = items or []
        # items in the library but NOT in the queue collection
        self.library_items = []
        self.existing_collections = collections or [
            {"key": "COLL", "data": {"name": "Reading Queue"}}
        ]
        self.created = []
        self.deleted = []
        self.tagged = []
        self.fail_create = False
        # Mirror pyzotero: 'deleted' is NOT a writable key by default,
        # so update_item rejects it until temp_keys is widened. This is
        # what the real API enforces; the trash path must widen first.
        self.temp_keys = {"key", "etag", "group_id", "updated"}

    def everything(self, value):
        return value

    def collections(self):
        return self.existing_collections

    def create_collections(self, payload):
        self.created_collections = getattr(self, "created_collections", [])
        self.created_collections.append(payload[0]["name"])
        # Register it so key->name lookups (collection membership on
        # receipts) resolve the collection we just created.
        self.existing_collections.append(
            {"key": "NEWCOLL", "data": {"name": payload[0]["name"]}}
        )
        return {"successful": {"0": {"key": "NEWCOLL"}}}

    def collection_items_top(self, key, **kwargs):
        # Emulate the Zotero Web API: insertion order is oldest-first,
        # so sort=dateAdded desc returns newest-first.
        if (
            kwargs.get("sort") == "dateAdded"
            and kwargs.get("direction") == "desc"
        ):
            return list(reversed(self.queue))
        return self.queue

    def top(self, **kwargs):
        return self.queue + self.library_items

    def item_template(self, item_type):
        # Mirror pyzotero + the current Zotero schema (v42): the
        # template carries exactly the fields valid for the type, and
        # every type this server creates — including book, report, and
        # kin — has a DOI field.
        return {"itemType": item_type, "DOI": ""}

    def attachment_simple(self, files, parentid=None):
        # Mirror pyzotero: every call creates a NEW child attachment
        # item, even when the file blob dedupes as "unchanged".
        self.attached = getattr(self, "attached", [])
        self.attached.append((parentid, list(files)))
        self.child_items = getattr(self, "child_items", {})
        self.child_items.setdefault(parentid, []).append(
            {"data": {"contentType": "application/pdf"}}
        )
        return {"success": {"0": "AKEY"}, "failure": {}, "unchanged": {}}

    def children(self, key):
        return getattr(self, "child_items", {}).get(key, [])

    def create_items(self, payload):
        if self.fail_create:
            return {"failed": {"0": {"message": "bad"}}, "successful": {}}
        self.created.extend(payload)
        return {"failed": {}, "successful": {"0": {"key": "NEWITEM"}}}

    def item(self, key):
        for item in self.queue + self.library_items:
            if item["key"] == key:
                return item
        return {"key": key, "data": {}}

    def items(self, q="", qmode=None, limit=None):
        needle = q.lower()
        return [
            item
            for item in self.queue + self.library_items
            if any(
                needle in str(value).lower() for value in item["data"].values()
            )
        ]

    def add_tags(self, item, tag):
        self.tagged.append((item["key"], tag))

    def _check_version(self, item_key, sent_version):
        # Mirror the Zotero API's If-Unmodified-Since-Version: every
        # write bumps the item's version, and a later write carrying a
        # stale version fails with 412. Sequential addto_collection
        # calls on one stale dict were failing in production while a
        # version-blind fake passed them.
        self.server_versions = getattr(self, "server_versions", {})
        current = self.server_versions.get(item_key, sent_version)
        if sent_version != current:
            raise ValueError(
                f"412 Precondition Failed: stale version for {item_key}"
            )
        self.server_versions[item_key] = current + 1

    def addto_collection(self, key, item):
        # Mirror pyzotero: PATCHes the server (version-guarded) and does
        # NOT mutate the passed item dict. Code that wants post-filing
        # membership must track it itself; a fake that mutated in place
        # masked exactly that bug. The PATCH is recorded for asserting.
        sent = item.get("version") or item["data"].get("version") or 0
        self._check_version(item["key"], sent)
        self.collection_adds = getattr(self, "collection_adds", [])
        self.collection_adds.append((key, item["key"]))

    def deletefrom_collection(self, key, item):
        self.uncollected = getattr(self, "uncollected", [])
        self.uncollected.append((key, item["key"]))
        item["data"]["collections"].remove(key)

    def delete_item(self, item):
        self.deleted.append(item["key"])
        self.queue = [i for i in self.queue if i["key"] != item["key"]]

    def update_item(self, payload):
        # Mirror pyzotero's check_items: reject keys not in the base
        # template plus temp_keys. This is what caught the real bug —
        # 'deleted' must be whitelisted via temp_keys before it sends.
        base = {"key", "version", "data", "collections", "tags"}
        for key in payload:
            if key not in base and key not in self.temp_keys:
                raise ValueError(f"Invalid keys present in item: {key}")
        self._check_version(payload["key"], payload.get("version") or 0)
        self.updated = getattr(self, "updated", [])
        self.updated.append(payload["key"])
        if "collections" in payload:
            self.collection_writes = getattr(self, "collection_writes", [])
            self.collection_writes.append(
                (payload["key"], list(payload["collections"]))
            )
        if payload.get("deleted"):
            self.trashed = getattr(self, "trashed", [])
            self.trashed.append(payload["key"])
            self.queue = [i for i in self.queue if i["key"] != payload["key"]]


@pytest.fixture
def fake_api(env, monkeypatch):
    fake = FakeZotero()
    monkeypatch.setattr(zotero_client, "_api", lambda: fake)
    return fake


def test_add_arxiv_paper_creates_preprint(fake_api, paper_factory):
    key, status = zotero_client.add_paper(paper_factory())
    assert (key, status) == ("NEWITEM", "created")
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
    fake_api.queue = [
        {
            "key": "EXISTING",
            "data": {
                "url": "https://arxiv.org/abs/2401.12345",
                "collections": ["COLL"],
            },
        }
    ]
    assert zotero_client.add_paper(paper_factory()) == (
        "EXISTING",
        "already_queued",
    )
    assert fake_api.created == []


def test_add_paper_dedups_on_doi_case_insensitive(fake_api, paper_factory):
    fake_api.queue = [
        {
            "key": "EXISTING",
            "data": {"DOI": "10.1038/NATURE12373", "collections": ["COLL"]},
        }
    ]
    paper = paper_factory(arxiv_id=None, doi="10.1038/nature12373")
    assert zotero_client.add_paper(paper) == ("EXISTING", "already_queued")


def test_add_paper_rejected_by_zotero(fake_api, paper_factory):
    fake_api.fail_create = True
    with pytest.raises(RuntimeError, match="rejected"):
        zotero_client.add_paper(paper_factory())


def test_matches_on_normalized_title(fake_api, paper_factory):
    fake_api.queue = [
        {
            "key": "K1",
            "data": {
                "title": "Observation of a New Particle!",
                "DOI": "10.1016/j.physletb.2012.08.020",
            },
        }
    ]
    arxiv_form = paper_factory(
        title="Observation of a new particle", arxiv_id="1207.7214", doi=None
    )
    item = zotero_client.find_item(arxiv_form)
    assert item is not None and item["key"] == "K1"


def test_matches_on_archive_id(fake_api, paper_factory):
    fake_api.queue = [
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
    fake_api.queue = [
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
    fake_api.queue = [
        {"key": "A", "data": {"tags": [{"tag": "sent-to-ereader"}]}},
        {"key": "B", "data": {"tags": [{"tag": zotero_client.NO_PDF_TAG}]}},
        {"key": "C", "data": {}},
    ]
    keys = [item["key"] for item in zotero_client.unsent_queue_items()]
    assert keys == ["C"]


def test_mark_sent_is_idempotent(fake_api):
    fake_api.queue = [
        {"key": "A", "data": {"tags": [{"tag": "sent-to-ereader"}]}},
        {"key": "B", "data": {"tags": []}},
    ]
    zotero_client.mark_sent("A")
    zotero_client.mark_sent("B")
    assert fake_api.tagged == [("B", "sent-to-ereader")]


def test_mark_no_pdf(fake_api):
    fake_api.queue = [{"key": "A", "data": {"tags": []}}]
    zotero_client.mark_no_pdf("A")
    assert fake_api.tagged == [("A", zotero_client.NO_PDF_TAG)]


def test_list_queue_statuses(fake_api):
    fake_api.queue = [
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
    fake_api.queue = [
        {"key": "A", "data": {"title": "Alpha", "DOI": "10.1000/alpha"}},
        {
            "key": "B",
            "data": {
                "title": "Beta",
                "url": "https://arxiv.org/abs/2401.12345",
            },
        },
    ]
    removed, misses, ambiguous = zotero_client.remove_by_refs(
        ["10.1000/alpha", "beta", "nonexistent"]
    )
    assert ambiguous == []
    assert [entry["title"] for entry in removed] == ["Alpha", "Beta"]
    assert misses == ["nonexistent"]
    # Moved to Zotero's Trash (restorable), never permanently deleted.
    assert fake_api.trashed == ["A", "B"]
    assert fake_api.deleted == []


def test_trash_requires_whitelisting_deleted_key(fake_api):
    # Guards the real-API bug: update_item rejects 'deleted' until
    # temp_keys is widened, so a trash that skipped the widening (or
    # sent the whole item) would raise, exactly as the live API did.
    fake_api.queue = [{"key": "A", "data": {"title": "Solo", "version": 5}}]
    # Sanity: the fake rejects 'deleted' before any widening.
    with pytest.raises(ValueError, match="Invalid keys"):
        fake_api.update_item({"key": "A", "version": 5, "deleted": 1})
    # remove_by_refs must succeed anyway — _trash_item widens first.
    removed, _, _ = zotero_client.remove_by_refs(["Solo"])
    assert [e["title"] for e in removed] == ["Solo"]
    assert fake_api.trashed == ["A"]


def test_remove_by_refs_refuses_ambiguous_title(fake_api):
    # A preprint and its published version share a title: removal must
    # stop and report both, never guess.
    fake_api.queue = [
        {
            "key": "PRE",
            "data": {"title": "Same Title", "archiveID": "arXiv:2401.12345"},
        },
        {
            "key": "PUB",
            "data": {"title": "Same Title", "DOI": "10.1000/pub"},
        },
    ]
    removed, misses, ambiguous = zotero_client.remove_by_refs(["same title"])
    assert removed == []
    assert misses == []
    assert ambiguous == [
        {
            "ref": "same title",
            "candidates": [
                {"key": "PRE", "id": "arXiv:2401.12345", "added": ""},
                {"key": "PUB", "id": "10.1000/pub", "added": ""},
            ],
        }
    ]
    # Nothing was touched.
    assert getattr(fake_api, "trashed", []) == []
    assert getattr(fake_api, "uncollected", []) == []


def test_remove_by_refs_specific_id_beats_duplicate_titles(fake_api):
    fake_api.queue = [
        {
            "key": "PRE",
            "data": {"title": "Same Title", "archiveID": "arXiv:2401.12345"},
        },
        {
            "key": "PUB",
            "data": {"title": "Same Title", "DOI": "10.1000/pub"},
        },
    ]
    removed, _, ambiguous = zotero_client.remove_by_refs(["10.1000/pub"])
    assert [e["title"] for e in removed] == ["Same Title"]
    assert ambiguous == []
    assert fake_api.trashed == ["PUB"]


def test_remove_by_refs_accepts_item_key(fake_api):
    # The disambiguation ids receipts advertise (Zotero item keys, for
    # items with no DOI/URL) must be consumable — never a dead end.
    fake_api.queue = [
        {"key": "IMJQVDDX", "data": {"title": "Only A Title"}},
    ]
    removed, misses, ambiguous = zotero_client.remove_by_refs(["IMJQVDDX"])
    assert [e["title"] for e in removed] == ["Only A Title"]
    assert misses == [] and ambiguous == []


def test_file_and_unfile_refuse_ambiguous_titles(fake_api):
    fake_api.existing_collections.append(
        {"key": "TOPIC", "data": {"name": "Topical"}}
    )
    fake_api.queue = [
        {"key": "A", "data": {"title": "Same Title", "collections": []}},
        {"key": "B", "data": {"title": "Same Title", "collections": []}},
    ]
    filed, misses, ambiguous = zotero_client.file_by_refs(
        ["same title"], "Topical"
    )
    assert filed == [] and misses == []
    assert [c["key"] for c in ambiguous[0]["candidates"]] == ["A", "B"]
    # Title-only items still get a usable (key) candidate.
    assert ambiguous[0]["candidates"][0]["id"] == "no other id"

    fake_api.queue[0]["data"]["collections"] = ["TOPIC"]
    fake_api.queue[1]["data"]["collections"] = ["TOPIC"]
    removed, misses, ambiguous = zotero_client.unfile_by_refs(
        ["same title"], "Topical"
    )
    assert removed == [] and misses == []
    assert len(ambiguous) == 1
    # An item key resolves the ambiguity.
    removed, _, ambiguous = zotero_client.unfile_by_refs(["B"], "Topical")
    assert removed == ["Same Title"]
    assert ambiguous == []


def test_remove_by_refs_reports_sent_state(env, fake_api):
    fake_api.queue = [
        {
            "key": "A",
            "data": {
                "title": "Already Read",
                "DOI": "10.1000/read",
                "tags": [{"tag": "sent-to-ereader"}],
            },
        }
    ]
    removed, _, _ = zotero_client.remove_by_refs(["10.1000/read"])
    assert removed == [
        {
            "title": "Already Read",
            "was_sent": True,
            "kept_in_library": False,
        }
    ]


def test_remove_keeps_items_filed_elsewhere(env, fake_api):
    fake_api.queue = [
        {
            "key": "A",
            "data": {
                "title": "Filed Paper",
                "DOI": "10.1000/filed",
                "collections": ["COLL", "TOPICAL"],
                "tags": [{"tag": "sent-to-ereader"}],
            },
        }
    ]
    removed, _, _ = zotero_client.remove_by_refs(["10.1000/filed"])
    assert removed == [
        {"title": "Filed Paper", "was_sent": True, "kept_in_library": True}
    ]
    # dropped from the queue collection, NOT trashed or deleted
    assert fake_api.deleted == []
    assert getattr(fake_api, "trashed", []) == []
    assert fake_api.uncollected == [("COLL", "A")]


def test_collection_key_rejects_empty_name(fake_api):
    with pytest.raises(ValueError, match="non-empty"):
        zotero_client.collection_key("   ", create=True)


def test_seed_ids_newest_first(fake_api):
    fake_api.queue = [
        {"key": "A", "data": {"archiveID": "arXiv:1706.03762"}},
        {"key": "B", "data": {"DOI": "10.1000/x"}},
        {"key": "C", "data": {"title": "No ids at all"}},
        {"key": "D", "data": {"archiveID": "arXiv:2312.00752"}},
    ]
    assert zotero_client.seed_ids(limit=2) == [
        "ArXiv:2312.00752",
        "DOI:10.1000/x",
    ]


def test_known_identities(fake_api):
    fake_api.queue = [
        {
            "key": "A",
            "data": {
                "title": "A Test Paper!",
                "DOI": "10.1000/Mixed",
                "archiveID": "arXiv:2401.12345",
            },
        }
    ]
    assert zotero_client.known_identities() == {
        "a test paper",
        "10.1000/mixed",
        "2401.12345",
    }


def test_read_paths_never_create_the_queue_collection(fake_api, paper_factory):
    fake_api.existing_collections = []  # brand-new library
    assert zotero_client.find_item(paper_factory()) is None
    assert zotero_client.list_queue() == []
    assert zotero_client.unsent_queue_items() == []
    assert getattr(fake_api, "created_collections", []) == []


def test_find_item_searches_whole_library(fake_api, paper_factory):
    # An item removed from the queue but kept in a topical collection
    # must still be found, or re-sends duplicate it.
    fake_api.library_items = [
        {
            "key": "KEPT",
            "data": {
                "title": "A Test Paper",
                "url": "https://arxiv.org/abs/2401.12345",
                "collections": ["TOPICAL"],
                "tags": [{"tag": "sent-to-ereader"}],
            },
        }
    ]
    item = zotero_client.find_item(paper_factory())
    assert item is not None and item["key"] == "KEPT"
    assert zotero_client.is_sent(item) is True


def test_requeue_of_kept_item_rejoins_queue(fake_api, paper_factory):
    kept = {
        "key": "KEPT",
        "data": {
            "title": "A Test Paper",
            "url": "https://arxiv.org/abs/2401.12345",
            "collections": ["TOPICAL"],
        },
    }
    fake_api.library_items = [kept]
    key, status = zotero_client.add_paper(paper_factory())
    assert (key, status) == ("KEPT", "requeued")
    # rejoined the queue collection (one union write), no duplicate
    assert ("KEPT", ["TOPICAL", "COLL"]) in fake_api.collection_writes
    assert fake_api.created == []


def test_remove_by_refs_rejects_empty_and_partial(fake_api):
    fake_api.queue = [
        {"key": "A", "data": {"title": "Alpha", "DOI": "10.1000/alpha"}},
        {"key": "B", "data": {"title": "Beta", "DOI": "10.1000/beta"}},
    ]
    removed, misses, _ = zotero_client.remove_by_refs(["", "  ", "10.1000"])
    assert removed == []
    assert misses == ["", "  ", "10.1000"]
    assert fake_api.deleted == []


def test_remove_by_refs_matches_archive_id(fake_api):
    fake_api.queue = [
        {
            "key": "A",
            "data": {
                "title": "Alpha",
                "url": "https://doi.org/10.65215/junk",
                "archiveID": "arXiv:2401.12345",
            },
        }
    ]
    removed, misses, _ = zotero_client.remove_by_refs(["arXiv:2401.12345"])
    assert [entry["title"] for entry in removed] == ["Alpha"]
    assert misses == []


def test_list_collections(fake_api):
    fake_api.existing_collections = [
        {
            "key": "P",
            "data": {"name": "ML", "parentCollection": None},
            "meta": {"numItems": 3},
        },
        {
            "key": "C",
            "data": {"name": "Transformers", "parentCollection": "P"},
            "meta": {"numItems": 1},
        },
    ]
    assert zotero_client.list_collections() == [
        {"name": "ML", "items": 3, "parent": None},
        {"name": "Transformers", "items": 1, "parent": "ML"},
    ]


def test_collection_key_case_insensitive_and_create(fake_api):
    fake_api.existing_collections = [
        {"key": "K1", "data": {"name": "Bayesian Methods"}}
    ]
    assert zotero_client.collection_key("bayesian methods") == "K1"
    assert zotero_client.collection_key("New Topic") is None
    assert zotero_client.collection_key("New Topic", create=True) == "NEWCOLL"


def test_add_paper_files_into_extra_collections(fake_api, paper_factory):
    _, status = zotero_client.add_paper(
        paper_factory(), collections=["Bayesian Methods"]
    )
    assert status == "created"
    # new item lands in the queue AND the topical collection
    assert fake_api.created[0]["collections"] == ["COLL", "NEWCOLL"]


def test_add_existing_paper_gains_collections(fake_api, paper_factory):
    fake_api.queue = [
        {
            "key": "EXISTING",
            "data": {
                "url": "https://arxiv.org/abs/2401.12345",
                "collections": ["COLL"],
            },
        }
    ]
    key, status = zotero_client.add_paper(
        paper_factory(), collections=["Topical"]
    )
    assert (key, status) == ("EXISTING", "already_queued")
    # One union write files it into the new topical collection while
    # keeping the existing queue membership.
    assert fake_api.collection_writes == [("EXISTING", ["COLL", "NEWCOLL"])]


def test_file_by_refs(fake_api):
    fake_api.queue = [
        {
            "key": "A",
            "data": {"title": "Alpha", "DOI": "10.1000/alpha"},
        }
    ]
    filed, misses, _ = zotero_client.file_by_refs(
        ["10.1000/alpha", "missing-ref"], "Topical"
    )
    assert filed == ["Alpha"]
    assert misses == ["missing-ref"]
    assert fake_api.collection_adds == [("NEWCOLL", "A")]


def test_file_by_refs_no_match_creates_no_collection(fake_api):
    fake_api.queue = [{"key": "A", "data": {"title": "Alpha"}}]
    filed, misses, _ = zotero_client.file_by_refs(["missing-ref"], "Phantom")
    assert filed == []
    assert misses == ["missing-ref"]
    # A call that files nothing must not create an empty collection.
    assert getattr(fake_api, "created_collections", []) == []


def test_unfile_by_refs_drops_only_that_membership(fake_api):
    fake_api.existing_collections.append(
        {"key": "TOPIC", "data": {"name": "Topical"}}
    )
    fake_api.queue = [
        {
            "key": "A",
            "data": {
                "title": "Alpha",
                "DOI": "10.1000/alpha",
                "collections": ["COLL", "TOPIC"],
            },
        }
    ]
    removed, misses, _ = zotero_client.unfile_by_refs(
        ["10.1000/alpha", "missing-ref"], "Topical"
    )
    assert removed == ["Alpha"]
    assert misses == ["missing-ref"]
    # Queue membership survives; only the topical collection is dropped.
    assert fake_api.queue[0]["data"]["collections"] == ["COLL"]
    assert fake_api.deleted == []


def test_unfile_by_refs_unknown_collection_raises(fake_api):
    with pytest.raises(ValueError, match="No collection named"):
        zotero_client.unfile_by_refs(["10.1/x"], "Nonexistent")


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


def test_scholarly_ref_for_key(fake_api, env):
    import paperboy.config

    env.setenv("ZOTERO_API_KEY", "k")
    env.setenv("ZOTERO_LIBRARY_ID", "1")
    env.setattr(paperboy.config, "_settings", None)
    fake_api.queue = [
        {
            "key": "S83KSPCA",
            "data": {"title": "Probe", "archiveID": "arXiv:1109.2378"},
        },
        {"key": "DOIONLY9", "data": {"title": "D", "DOI": "10.1/d"}},
        {"key": "TITLEON1", "data": {"title": "Only A Title Here"}},
    ]
    assert zotero_client.scholarly_ref_for_key("S83KSPCA") == "1109.2378"
    assert zotero_client.scholarly_ref_for_key("DOIONLY9") == "10.1/d"
    assert (
        zotero_client.scholarly_ref_for_key("TITLEON1") == "Only A Title Here"
    )
    # Non-key-shaped refs and unknown keys fall through to resolution.
    assert zotero_client.scholarly_ref_for_key("2401.12345") is None
    assert zotero_client.scholarly_ref_for_key("some paper title") is None


def test_scholarly_ref_for_unknown_key_returns_none(fake_api, env, monkeypatch):
    import paperboy.config

    env.setenv("ZOTERO_API_KEY", "k")
    env.setenv("ZOTERO_LIBRARY_ID", "1")
    env.setattr(paperboy.config, "_settings", None)

    from pyzotero import zotero_errors

    def missing(key):
        raise zotero_errors.ResourceNotFoundError("no such item")

    monkeypatch.setattr(fake_api, "item", missing)
    assert zotero_client.scholarly_ref_for_key("NOTAKEY1") is None


def test_scholarly_ref_outage_raises_transient(fake_api, env, monkeypatch):
    import paperboy.config

    env.setenv("ZOTERO_API_KEY", "k")
    env.setenv("ZOTERO_LIBRARY_ID", "1")
    env.setattr(paperboy.config, "_settings", None)

    def outage(key):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fake_api, "item", outage)
    with pytest.raises(zotero_client.ZoteroUnavailableError, match="retry"):
        zotero_client.scholarly_ref_for_key("NOTAKEY1")


def test_matching_items_accepts_lowercase_key(fake_api):
    fake_api.queue = [{"key": "IMJQVDDX", "data": {"title": "T"}}]
    removed, misses, _ = zotero_client.remove_by_refs(["imjqvddx"])
    assert [e["title"] for e in removed] == ["T"]
    assert misses == []


# --- catalog_paper (track-only) ----------------------------------------


def test_catalog_paper_does_not_queue(fake_api, paper_factory):
    key, status, names = zotero_client.catalog_paper(
        paper_factory(), collections=["Theory"]
    )
    assert (key, status) == ("NEWITEM", "created")
    item = fake_api.created[0]
    # Filed into the topical collection only, never the Reading Queue.
    assert item["collections"] == ["NEWCOLL"]
    assert "COLL" not in item["collections"]
    assert names == ["Theory"]


def test_catalog_paper_existing_reports_membership(fake_api, paper_factory):
    fake_api.library_items = [
        {
            "key": "EX",
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1/x",
                "title": "A Test Paper",
                "collections": ["COLL"],
            },
        }
    ]
    key, status, names = zotero_client.catalog_paper(
        paper_factory(arxiv_id=None, doi="10.1/x")
    )
    assert (key, status) == ("EX", "existing")
    assert names == ["Reading Queue"]


# --- add_book ----------------------------------------------------------


def test_add_book_creates_book_item(fake_api):
    book = Book(
        title="Lebesgue Measure",
        authors=["Gail Nelson"],
        year="2015",
        publisher="AMS",
        isbn="9781470421991",
        num_pages="221",
    )
    key, status, _ = zotero_client.add_book(book, collections=["Textbooks"])
    assert (key, status) == ("NEWITEM", "created")
    item = fake_api.created[0]
    assert item["itemType"] == "book"
    assert item["ISBN"] == "9781470421991"
    assert item["publisher"] == "AMS"
    assert item["numPages"] == "221"
    assert item["DOI"] == ""  # DOI-less book leaves the field empty
    assert item["collections"] == ["NEWCOLL"]


def test_add_book_stores_doi_in_doi_field(fake_api):
    # The current Zotero schema gives book a real DOI field.
    book = Book(title="X", authors=[], doi="10.1090/stml/078")
    zotero_client.add_book(book)
    assert fake_api.created[0]["DOI"] == "10.1090/stml/078"
    assert "extra" not in fake_api.created[0]


def test_add_book_doi_falls_back_to_extra_without_field(fake_api, monkeypatch):
    # An old cached schema whose book template lacks DOI: the DOI must
    # still land somewhere searchable.
    monkeypatch.setattr(
        fake_api, "item_template", lambda item_type: {"itemType": item_type}
    )
    book = Book(title="X", authors=[], doi="10.1090/stml/078")
    zotero_client.add_book(book)
    assert fake_api.created[0]["extra"] == "DOI: 10.1090/stml/078"


def test_add_book_dedups_on_isbn_10_13_equivalence(fake_api):
    fake_api.library_items = [
        {
            "key": "BK",
            "data": {
                "itemType": "book",
                "ISBN": "978-0-306-40615-7",
                "title": "Something",
                "collections": [],
            },
        }
    ]
    # Same edition, given as ISBN-10 this time.
    book = Book(title="Something", authors=[], isbn="0-306-40615-2")
    key, status, _ = zotero_client.add_book(book)
    assert (key, status) == ("BK", "existing")
    assert fake_api.created == []


def test_paper_does_not_dedup_against_a_book(fake_api, paper_factory):
    # A paper titled the same as a book must NOT match the book.
    fake_api.library_items = [
        {
            "key": "BK",
            "data": {
                "itemType": "book",
                "title": "Deep Learning",
                "collections": [],
            },
        }
    ]
    paper = paper_factory(title="Deep Learning", arxiv_id=None, doi="10.1/dl")
    assert zotero_client.find_item(paper) is None


@pytest.mark.parametrize(
    "grey_type",
    ["report", "thesis", "manuscript", "document"],
)
def test_paper_does_not_title_match_grey_lit_item(
    fake_api, paper_factory, grey_type
):
    # A paper resolved by title must NOT fuzzy-match a same-titled item
    # that attach_pdf can create (report/thesis/...), which would clobber
    # that grey-lit item's delivery state.
    fake_api.library_items = [
        {
            "key": "GREY",
            "data": {
                "itemType": grey_type,
                "title": "Deep Learning",
                "collections": [],
            },
        }
    ]
    # No shared id — only the fuzzy title bridge could (wrongly) match.
    paper = paper_factory(title="Deep Learning", arxiv_id=None, doi="10.1/dl")
    assert zotero_client.find_item(paper) is None


def test_paper_title_matches_conference_paper(fake_api, paper_factory):
    # Connector-saved CS papers land as conferencePaper, usually with no
    # DOI/arXiv id — the title bridge is their only dedup path. Excluding
    # them re-delivered already-sent papers after an upgrade.
    fake_api.library_items = [
        {
            "key": "CONF",
            "data": {
                "itemType": "conferencePaper",
                "title": "Attention Is All You Need",
                "collections": [],
            },
        }
    ]
    paper = paper_factory(
        title="Attention Is All You Need", arxiv_id=None, doi="10.1/aiayn"
    )
    item = zotero_client.find_item(paper)
    assert item is not None and item["key"] == "CONF"


def test_exact_doi_still_dedups_across_item_types(fake_api, paper_factory):
    # An exact DOI/arXiv id is identity-safe: it should still match even
    # when the stored item is not a preprint/journalArticle.
    fake_api.library_items = [
        {
            "key": "R1",
            "data": {
                "itemType": "report",
                "DOI": "10.1/shared",
                "title": "Whatever",
                "collections": [],
            },
        }
    ]
    paper = paper_factory(title="Different", arxiv_id=None, doi="10.1/shared")
    item = zotero_client.find_item(paper)
    assert item is not None and item["key"] == "R1"


# --- attach_pdf_item ---------------------------------------------------


def test_attach_pdf_item_creates_and_attaches(fake_api):
    key, status, attached = zotero_client.attach_pdf_item(
        item_type="report",
        title="A Working Paper",
        authors=["Jane Doe"],
        pdf_path="/tmp/A_Working_Paper.pdf",
        year="2024",
        collections=["Grey Lit"],
    )
    assert (key, status) == ("NEWITEM", "created")
    assert attached is True
    item = fake_api.created[0]
    assert item["itemType"] == "report"
    assert item["collections"] == ["NEWCOLL"]
    assert fake_api.attached == [("NEWITEM", ["/tmp/A_Working_Paper.pdf"])]


def test_attach_pdf_item_reuses_same_type_and_title(fake_api):
    # Retry safety: a re-run after a failed upload attaches to the item
    # already created instead of minting a duplicate that nothing
    # (by design) will ever title-dedup.
    fake_api.library_items = [
        {
            "key": "GREY",
            "data": {
                "itemType": "report",
                "title": "A Working Paper",
                "collections": [],
            },
        }
    ]
    key, status, attached = zotero_client.attach_pdf_item(
        item_type="report",
        title="A Working Paper",
        authors=["Jane Doe"],
        pdf_path="/tmp/x.pdf",
    )
    assert (key, status) == ("GREY", "existing")
    assert attached is True
    assert fake_api.created == []
    assert fake_api.attached == [("GREY", ["/tmp/x.pdf"])]


def test_attach_pdf_item_does_not_reuse_other_types(fake_api):
    # Same title but a different item type is a different work.
    fake_api.library_items = [
        {
            "key": "BK",
            "data": {
                "itemType": "book",
                "title": "A Working Paper",
                "collections": [],
            },
        }
    ]
    key, status, _ = zotero_client.attach_pdf_item(
        item_type="report",
        title="A Working Paper",
        authors=[],
        pdf_path="/tmp/x.pdf",
    )
    assert (key, status) == ("NEWITEM", "created")


def test_attach_file_swallows_upload_exception(fake_api, monkeypatch):
    # A raised upload failure after create_items succeeded must read as
    # "attachment did not upload", never "could not create the item".
    def boom(files, parentid=None):
        raise RuntimeError("upload broke mid-flight")

    monkeypatch.setattr(fake_api, "attachment_simple", boom)
    key, status, attached = zotero_client.attach_pdf_item(
        item_type="report",
        title="A Working Paper",
        authors=[],
        pdf_path="/tmp/x.pdf",
    )
    assert (key, status) == ("NEWITEM", "created")
    assert attached is False


def test_item_collection_names(fake_api):
    fake_api.existing_collections.append(
        {"key": "K2", "data": {"name": "Theory"}}
    )
    item = {"data": {"collections": ["COLL", "K2"]}}
    assert zotero_client.item_collection_names(item) == [
        "Reading Queue",
        "Theory",
    ]


# --- review-round fixes -------------------------------------------------


def test_book_isbn_field_with_multiple_isbns_dedups(fake_api):
    # Zotero's MARC translator stores several ISBNs space-separated in
    # the one field; each token must be tried, not the concatenation.
    fake_api.library_items = [
        {
            "key": "BK",
            "data": {
                "itemType": "book",
                "ISBN": "9780306406157 0306406152",
                "title": "Something",
                "collections": [],
            },
        }
    ]
    book = Book(title="Something", authors=[], isbn="978-0-306-40615-7")
    key, status, _ = zotero_client.add_book(book)
    assert (key, status) == ("BK", "existing")
    assert fake_api.created == []


def test_catalog_existing_receipt_includes_newly_filed_collection(
    fake_api, paper_factory
):
    # pyzotero's addto_collection does not mutate the passed item dict;
    # the receipt must still report the post-filing membership.
    fake_api.existing_collections.append(
        {"key": "TH", "data": {"name": "Theory"}}
    )
    fake_api.library_items = [
        {
            "key": "EX",
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1/x",
                "title": "A Test Paper",
                "collections": [],
            },
        }
    ]
    _, status, names = zotero_client.catalog_paper(
        paper_factory(arxiv_id=None, doi="10.1/x"), collections=["Theory"]
    )
    assert status == "existing"
    assert names == ["Theory"]


def test_add_paper_skips_receipt_names_on_queue_path(
    fake_api, paper_factory, monkeypatch
):
    # queue_papers/send_papers discard collection names; computing them
    # cost a full post-mutation collections fetch per paper.
    def forbidden(keys):
        raise AssertionError("names must not be computed on the queue path")

    monkeypatch.setattr(zotero_client, "_receipt_names", forbidden)
    key, status = zotero_client.add_paper(paper_factory())
    assert (key, status) == ("NEWITEM", "created")


def test_receipt_names_degrade_on_zotero_failure(fake_api, monkeypatch):
    # Names are receipt garnish computed after the mutation — a Zotero
    # hiccup there must degrade to [], never destroy the receipt.
    def boom():
        raise RuntimeError("zotero down")

    monkeypatch.setattr(zotero_client, "_collections_raw", boom)
    assert zotero_client._receipt_names(["COLL"]) == []


# --- round-A fixes -------------------------------------------------------


def test_multi_collection_filing_is_one_versioned_write(
    fake_api, paper_factory
):
    # Two sequential addto_collection calls on one stale dict 412
    # against the real API; filing into several collections must be a
    # single union write. The fake's version check enforces this.
    fake_api.existing_collections += [
        {"key": "T1", "data": {"name": "Theory"}},
        {"key": "T2", "data": {"name": "ML"}},
    ]
    fake_api.library_items = [
        {
            "key": "EX",
            "version": 7,
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1/x",
                "title": "A Test Paper",
                "collections": [],
            },
        }
    ]
    _, status, names = zotero_client.catalog_paper(
        paper_factory(arxiv_id=None, doi="10.1/x"),
        collections=["Theory", "ML"],
    )
    assert status == "existing"
    assert names == ["Theory", "ML"]
    assert fake_api.collection_writes == [("EX", ["T1", "T2"])]


def test_filing_nothing_new_writes_nothing(fake_api, paper_factory):
    # An item already in every requested collection must not be PATCHed
    # at all — a no-op write still bumps versions and burns quota.
    fake_api.library_items = [
        {
            "key": "EX",
            "data": {
                "itemType": "journalArticle",
                "DOI": "10.1/x",
                "title": "A Test Paper",
                "collections": [],
            },
        }
    ]
    _, status, _ = zotero_client.catalog_paper(
        paper_factory(arxiv_id=None, doi="10.1/x")
    )
    assert status == "existing"
    assert getattr(fake_api, "collection_writes", []) == []


def test_book_isbn_field_with_internal_spaces_dedups(fake_api):
    # A single ISBN written with internal spaces ("978 0 306 40615 7")
    # must dedup — tokenizing alone would shred it into invalid pieces.
    fake_api.library_items = [
        {
            "key": "BK",
            "data": {
                "itemType": "book",
                "ISBN": "978 0 306 40615 7",
                "title": "Something",
                "collections": [],
            },
        }
    ]
    book = Book(title="Something", authors=[], isbn="9780306406157")
    key, status, _ = zotero_client.add_book(book)
    assert (key, status) == ("BK", "existing")


def test_book_isbn_matches_any_stored_token(fake_api):
    # MARC records repeat 020 for hbk/pbk/ebook ISBNs; the incoming
    # ISBN may match any of them, not just the first.
    fake_api.library_items = [
        {
            "key": "BK",
            "data": {
                "itemType": "book",
                "ISBN": "9781470421991 9780306406157",
                "title": "Something",
                "collections": [],
            },
        }
    ]
    book = Book(title="Something", authors=[], isbn="9780306406157")
    key, status, _ = zotero_client.add_book(book)
    assert (key, status) == ("BK", "existing")
    assert fake_api.created == []


def test_file_by_refs_two_refs_same_item_files_once(fake_api):
    # A ref by DOI and a ref by title can name the same queue item; the
    # second must not trigger a stale-version write (412) or a double
    # count — the item is filed once and the extra ref is consumed.
    fake_api.queue = [
        {
            "key": "A",
            "data": {"title": "Alpha", "DOI": "10.1000/alpha"},
        }
    ]
    filed, misses, ambiguous = zotero_client.file_by_refs(
        ["10.1000/alpha", "Alpha"], "Topical"
    )
    assert filed == ["Alpha"]
    assert misses == []
    assert ambiguous == []
    assert fake_api.collection_adds == [("NEWCOLL", "A")]


def test_unfile_by_refs_two_refs_same_item_removes_once(fake_api):
    fake_api.existing_collections.append(
        {"key": "TOPIC", "data": {"name": "Topical"}}
    )
    fake_api.queue = [
        {
            "key": "A",
            "data": {
                "title": "Alpha",
                "DOI": "10.1000/alpha",
                "collections": ["TOPIC"],
            },
        }
    ]
    removed, misses, ambiguous = zotero_client.unfile_by_refs(
        ["10.1000/alpha", "Alpha"], "Topical"
    )
    assert removed == ["Alpha"]
    assert misses == []
    assert ambiguous == []
    assert fake_api.uncollected == [("TOPIC", "A")]


# --- round-D fixes -------------------------------------------------------


def test_partially_consumed_ambiguity_still_refuses(fake_api):
    # Item A filed via its key; the shared title still matches A and B.
    # Acting on B by elimination would be guessing — the refusal
    # contract holds even when one candidate was already handled.
    fake_api.queue = [
        {"key": "AAAA1111", "data": {"title": "Deep Learning"}},
        {"key": "BBBB2222", "data": {"title": "Deep Learning"}},
    ]
    filed, _misses, ambiguous = zotero_client.file_by_refs(
        ["AAAA1111", "Deep Learning"], "Topical"
    )
    assert filed == ["Deep Learning"]  # A only
    assert fake_api.collection_adds == [("NEWCOLL", "AAAA1111")]
    assert len(ambiguous) == 1  # the title ref refused, both candidates
    assert {c["key"] for c in ambiguous[0]["candidates"]} == {
        "AAAA1111",
        "BBBB2222",
    }


def test_remove_partially_consumed_ambiguity_still_refuses(fake_api):
    fake_api.queue = [
        {"key": "AAAA1111", "data": {"title": "Deep Learning"}},
        {"key": "BBBB2222", "data": {"title": "Deep Learning"}},
    ]
    removed, _misses, ambiguous = zotero_client.remove_by_refs(
        ["AAAA1111", "Deep Learning"]
    )
    assert len(removed) == 1  # A only; B untouched
    assert len(ambiguous) == 1
    assert fake_api.trashed == ["AAAA1111"]


def test_remove_by_refs_aliased_refs_remove_once(fake_api):
    # DOI ref and title ref naming the same item: removed once, second
    # ref consumed silently (not a miss).
    fake_api.queue = [
        {"key": "A", "data": {"title": "Alpha", "DOI": "10.1000/alpha"}},
    ]
    removed, misses, _ambiguous = zotero_client.remove_by_refs(
        ["10.1000/alpha", "Alpha"]
    )
    assert len(removed) == 1
    assert misses == []
    assert fake_api.trashed == ["A"]


def test_attach_pdf_item_rerun_does_not_duplicate_attachment(fake_api):
    # First run creates the item and attaches; a re-run reuses the item
    # AND skips the upload — pyzotero would create a duplicate child
    # attachment item per call.
    first = zotero_client.attach_pdf_item(
        item_type="report",
        title="A Working Paper",
        authors=["Jane Doe"],
        pdf_path="/tmp/x.pdf",
    )
    assert first == ("NEWITEM", "created", True)
    fake_api.library_items = [
        {
            "key": "NEWITEM",
            "data": {
                "itemType": "report",
                "title": "A Working Paper",
                "collections": [],
            },
        }
    ]
    second = zotero_client.attach_pdf_item(
        item_type="report",
        title="A Working Paper",
        authors=["Jane Doe"],
        pdf_path="/tmp/x.pdf",
    )
    assert second == ("NEWITEM", "existing", True)
    assert len(fake_api.attached) == 1  # no second upload


# --- round-F fixes -------------------------------------------------------


def test_distinct_non_latin_titles_do_not_merge(fake_api, paper_factory):
    # Both titles used to normalize to "" and false-match everywhere.
    fake_api.queue = [
        {
            "key": "RU1",
            "data": {
                "itemType": "journalArticle",
                "title": "Квантовая механика и интегралы",
            },
        }
    ]
    other = paper_factory(
        title="Введение в теорию вероятностей", arxiv_id=None, doi=None
    )
    assert zotero_client.find_item(other) is None


def test_same_non_latin_title_still_dedups(fake_api, paper_factory):
    fake_api.queue = [
        {
            "key": "RU1",
            "data": {
                "itemType": "journalArticle",
                "title": "Квантовая механика и интегралы",
            },
        }
    ]
    same = paper_factory(
        title="Квантовая механика и интегралы!", arxiv_id=None, doi=None
    )
    item = zotero_client.find_item(same)
    assert item is not None and item["key"] == "RU1"


def test_degenerate_titles_never_match(fake_api, paper_factory):
    # All-punctuation titles normalize to ""; two of them are NOT the
    # same work.
    fake_api.queue = [
        {
            "key": "P1",
            "data": {"itemType": "journalArticle", "title": "???"},
        }
    ]
    other = paper_factory(title="!!!", arxiv_id=None, doi=None)
    assert zotero_client.find_item(other) is None


def test_find_attach_target_refuses_degenerate_title(fake_api):
    fake_api.library_items = [
        {"key": "X1", "data": {"itemType": "report", "title": "***"}}
    ]
    assert zotero_client.find_attach_target("report", "!!!") is None


def test_untitled_item_receipts_fall_back_to_key(fake_api):
    # The real API serves title: "" (key present) for untitled items;
    # receipts must fall back to the item key, not render blank.
    fake_api.queue = [
        {"key": "UNTITLED1", "data": {"title": "", "collections": []}}
    ]
    entries = zotero_client.list_queue()
    assert entries[0]["title"] == "UNTITLED1"
