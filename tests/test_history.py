from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import history, main

RESULT = {
    "video_uri": "gs://test-videos/uploads/a",
    "reportable_count": 2,
    "reportable": [
        {"vehicle_id": "v1", "legal_basis": "Act 333, s.43(1)"},
        {"vehicle_id": "v2", "legal_basis": "Act 333, s.42(1)"},
    ],
    "verdict": "Two vehicles should be reported.",
    "rounds_used": 3,
    "coverage": {"duration_seconds": 255.5, "windows_analysed": 5},
    "evidence": {"timeline": [{"start_seconds": 0}], "vehicles": [], "legal_analysis": {}},
}


class FakeDoc:
    def __init__(self, data):
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return self._data


class FakeRef:
    def __init__(self, store, key):
        self.store, self.key = store, key

    async def set(self, data):
        self.store[self.key] = data

    async def get(self):
        return FakeDoc(self.store.get(self.key))

    async def delete(self):
        self.store.pop(self.key, None)


class FakeQuery:
    def __init__(self, rows):
        self.rows = rows

    def order_by(self, field, direction=None):
        return FakeQuery(sorted(self.rows, key=lambda r: r[field], reverse=True))

    def limit(self, n):
        self._limit = n
        return self

    def start_after(self, cursor):
        # Firestore applies the cursor to the ordered set, then the limit; the
        # real client orders, cursors and limits in that sequence whatever the
        # call order, so the fake must not truncate before cursoring.
        after = cursor["created_at"]
        kept = FakeQuery([r for r in self.rows if r["created_at"] < after])
        kept._limit = getattr(self, "_limit", None)
        return kept

    async def stream(self):
        for row in self.rows[: getattr(self, "_limit", None)]:
            yield FakeDoc(row)

    def __aiter__(self):
        return self.stream()


class FakeCollection:
    def __init__(self, store):
        self.store = store

    def document(self, key):
        return FakeRef(self.store, key)

    def order_by(self, field, direction=None):
        return FakeQuery(list(self.store.values())).order_by(field, direction)


class FakeDb:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        return FakeCollection(self.collections.setdefault(name, {}))


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(history, "client", lambda: fake)
    return fake


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VIDEO_BUCKET", "test-videos")
    return TestClient(main.app)


def test_a_summary_carries_only_what_a_list_shows():
    at = datetime.now(timezone.utc)
    summary = history.summarise("abc", RESULT, at)
    assert summary["reportable_count"] == 2
    assert summary["provisions"] == ["Act 333, s.42(1)", "Act 333, s.43(1)"]
    assert summary["duration_seconds"] == 255.5
    # The evidence is what makes a record large; it must not reach the summary.
    assert "evidence" not in summary


def test_a_long_verdict_is_truncated_in_the_summary():
    summary = history.summarise("abc", {"verdict": "x" * 900}, datetime.now(timezone.utc))
    assert len(summary["verdict"]) == 500


def test_a_review_with_no_findings_summarises_cleanly():
    summary = history.summarise("abc", {}, datetime.now(timezone.utc))
    assert summary["reportable_count"] == 0 and summary["provisions"] == []


@pytest.mark.anyio
async def test_saving_writes_both_a_summary_and_a_record(db):
    review_id = await history.save(RESULT)
    assert db.collections[history.SUMMARIES][review_id]["reportable_count"] == 2
    assert db.collections[history.RECORDS][review_id]["evidence"] == RESULT["evidence"]


@pytest.mark.anyio
async def test_a_storage_failure_does_not_lose_the_caller_their_review(monkeypatch):
    class Broken:
        def collection(self, name):
            raise RuntimeError("firestore is down")

    monkeypatch.setattr(history, "client", lambda: Broken())
    # The review already cost minutes of model time; it must still be returned.
    assert await history.save(RESULT)


@pytest.mark.anyio
async def test_recent_returns_newest_first(db):
    base = datetime.now(timezone.utc)
    for i in range(3):
        db.collections.setdefault(history.SUMMARIES, {})[f"r{i}"] = {
            "review_id": f"r{i}",
            "created_at": base + timedelta(minutes=i),
        }
    rows = await history.recent(limit=10)
    assert [r["review_id"] for r in rows] == ["r2", "r1", "r0"]


@pytest.mark.anyio
async def test_paging_continues_after_the_cursor(db):
    base = datetime.now(timezone.utc)
    for i in range(3):
        db.collections.setdefault(history.SUMMARIES, {})[f"r{i}"] = {
            "review_id": f"r{i}",
            "created_at": base + timedelta(minutes=i),
        }
    first = await history.recent(limit=2)
    rest = await history.recent(limit=2, before=first[-1]["created_at"])
    assert [r["review_id"] for r in rest] == ["r0"]


@pytest.mark.anyio
async def test_the_page_size_is_capped(db):
    for i in range(5):
        db.collections.setdefault(history.SUMMARIES, {})[f"r{i}"] = {
            "review_id": f"r{i}",
            "created_at": datetime.now(timezone.utc) + timedelta(seconds=i),
        }
    assert len(await history.recent(limit=99999)) <= history.MAX_PAGE


@pytest.mark.anyio
async def test_getting_and_deleting_a_review(db):
    review_id = await history.save(RESULT)
    assert (await history.get(review_id))["verdict"] == RESULT["verdict"]
    assert await history.delete(review_id) is True
    assert await history.get(review_id) is None
    # Deleting what is already gone reports that rather than pretending.
    assert await history.delete(review_id) is False


def test_reading_an_unknown_review_is_a_404(client, db):
    assert client.get("/v1/reviews/does-not-exist").status_code == 404


def test_a_malformed_cursor_is_rejected(client, db):
    assert client.get("/v1/reviews?before=not-a-date").status_code == 422


def test_a_cursor_whose_plus_became_a_space_still_works(client, db):
    """A query string decodes "+" as a space, so an offset arrives broken."""
    assert client.get("/v1/reviews?before=2026-10-04T12:09:54.711370 00:00").status_code == 200
    assert client.get("/v1/reviews?before=2026-10-04T12:09:54.711370%2B00:00").status_code == 200
