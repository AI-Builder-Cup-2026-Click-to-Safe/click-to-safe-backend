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

    async def set(self, data, merge=False):
        self.store[self.key] = {**(self.store.get(self.key) or {}), **data} if merge else data

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


def test_a_summary_includes_the_server_cost_when_available():
    at = datetime.now(timezone.utc)
    priced = history.summarise("abc", {**RESULT, "estimated_ai_cost_usd": 0.04460925}, at)
    old = history.summarise("old", RESULT, at)
    assert priced["estimated_ai_cost_usd"] == 0.04460925
    assert old["estimated_ai_cost_usd"] is None


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


def test_playback_urls_reach_the_case_the_dashboard_reads(monkeypatch):
    """The case is built at save time; a signed URL only exists at read time."""
    monkeypatch.setattr(history, "playable", lambda uri: f"https://signed/{uri}" if uri else None)
    record = {
        "video_uri": "gs://b/uploads/a",
        "annotated_video_uri": "gs://b/annotated/a",
        "excerpt_video_uri": "gs://b/excerpt/a",
        "case": {"id": "RR-1", "evidence": {"annotatedVideoUrl": None, "tracks": []}},
        "decision_trail": {"caseId": "RR-1", "excerptVideoUrl": None},
    }
    out = history.with_playback(record)
    assert out["case"]["evidence"]["annotatedVideoUrl"] == "https://signed/gs://b/annotated/a"
    assert out["case"]["evidence"]["originalVideoUrl"] == "https://signed/gs://b/uploads/a"
    assert out["decision_trail"]["excerptVideoUrl"] == "https://signed/gs://b/excerpt/a"
    # What the case already carried is left alone.
    assert out["case"]["evidence"]["tracks"] == []


def test_a_record_without_a_case_still_reads_back(monkeypatch):
    monkeypatch.setattr(history, "playable", lambda uri: None)
    out = history.with_playback({"video_uri": "gs://b/uploads/a"})
    assert out["case"] is None and out["decision_trail"] is None


def summary(plate=None, status="ready", codes=(), days_ago=0):
    from datetime import timedelta as td

    return {
        "plate": plate,
        "reporting_status": status,
        "offence_codes": list(codes),
        "created_at": datetime.now(timezone.utc) - td(days=days_ago),
    }


def test_only_cases_a_person_sent_are_counted_as_reports():
    # A case the model produced but nobody sent is not a report.
    out = history.tally(
        [summary("AAA 1", "sent"), summary("BBB 2", "ready"), summary("CCC 3", "opened")],
        "2026-10-08",
    )
    assert out["snitchedCount"] == 1


def test_one_plate_reported_twice_is_one_report():
    out = history.tally([summary("AAA 1", "sent"), summary("AAA 1", "sent")], "2026-10-08")
    assert out["snitchedCount"] == 1


def test_sightings_count_every_case_not_distinct_plates():
    # The same plate in three cases is one snitch but three sightings.
    out = history.tally([summary("AAA 1") for _ in range(3)], "2026-10-08")
    assert out["mostSpotted"] == {"plate": "AAA 1", "count": 3}


def test_offences_are_counted_per_occurrence():
    out = history.tally(
        [summary("A 1", codes=["queue_cut"]), summary("B 2", codes=["queue_cut", "red_light"])],
        "2026-10-08",
    )
    assert out["offenceOccurrences"] == {"queue_cut": 2, "red_light": 1}


def test_a_case_without_a_plate_is_not_a_sighting():
    out = history.tally([summary(None, "sent", ["road_rage"])], "2026-10-08")
    # Not a sighting, but still a report a person sent.
    assert out["mostSpotted"] is None and out["snitchedCount"] == 1
    # The offence still happened, so it is still counted.
    assert out["offenceOccurrences"] == {"road_rage": 1}


def test_an_empty_history_counts_zero():
    out = history.tally([], "2026-10-08")
    assert out["snitchedCount"] == 0 and out["mostSpotted"] is None
    assert out["offenceOccurrences"] == {}


def test_fines_are_never_claimed_without_an_authority():
    # Nothing is wired to an enforcement feed, so any number would be invented.
    assert history.tally([summary("A 1", "sent")], "2026-10-08")["billedCount"] == 0


def test_the_counters_are_never_marked_as_demo():
    assert history.tally([], "2026-10-08")["isDemo"] is False


@pytest.mark.parametrize("period,scoped", [("today", True), ("week", True), ("all", False)])
def test_a_period_scopes_the_window(period, scoped):
    assert (history.period_start(period) is not None) is scoped


def test_an_unknown_period_is_rejected(client, db):
    assert client.get("/v1/impact?period=fortnight").status_code == 422


@pytest.mark.anyio
async def test_cost_survives_history_storage_and_detail_read(db):
    review_id = await history.save({**RESULT, "estimated_ai_cost_usd": 0.002049})
    assert (await history.get(review_id))["estimated_ai_cost_usd"] == 0.002049


def test_reports_without_a_plate_each_count_once():
    out = history.tally(
        [summary(None, "sent"), summary(None, "sent"), summary("AAA 1", "sent")], "2026-10-08"
    )
    assert out["snitchedCount"] == 3


@pytest.mark.anyio
async def test_a_typed_plate_fills_one_the_footage_did_not_give(db):
    db.collections[history.RECORDS] = {"r1": {"case": {"reporting": {}}}, "r2": {"case": {}}}
    db.collections[history.SUMMARIES] = {"r1": {"plate": None}, "r2": {"plate": "WC 7178 W"}}
    assert await history.set_reporting_status("r1", "sent", " wxy  2048 ")
    assert await history.set_reporting_status("r2", "sent", "OTHER 1")
    summaries = db.collections[history.SUMMARIES]
    assert summaries["r1"] == {"plate": "WXY 2048", "reporting_status": "sent"}
    # What the footage showed is never replaced by what was typed.
    assert summaries["r2"]["plate"] == "WC 7178 W"
