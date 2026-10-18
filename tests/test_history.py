from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import Aborted

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
    def __init__(self, data, reference=None, document_id=None, update_time=None):
        self._data = deepcopy(data)
        self.reference = reference
        self.id = document_id
        self.update_time = update_time

    @property
    def exists(self):
        return self._data is not None

    def to_dict(self):
        return deepcopy(self._data)


class FakeRef:
    def __init__(self, store, key, versions):
        self.store, self.key = store, key
        self.versions = versions

    async def set(self, data, merge=False):
        self.store[self.key] = deepcopy(
            {**(self.store.get(self.key) or {}), **data} if merge else data
        )
        self.versions[self.key] = self.versions.get(self.key, 0) + 1

    async def get(self, transaction=None):
        version = self.versions.get(self.key, 0)
        if transaction is not None:
            transaction.reads.append((self, version))
        return FakeDoc(self.store.get(self.key), update_time=version)

    async def update(self, data):
        for path, value in data.items():
            target = self.store[self.key]
            parts = path.split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = value
        self.versions[self.key] = self.versions.get(self.key, 0) + 1

    async def delete(self):
        self.store.pop(self.key, None)
        self.versions[self.key] = self.versions.get(self.key, 0) + 1


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
    def __init__(self, store, versions):
        self.store = store
        self.versions = versions

    def document(self, key):
        return FakeRef(self.store, key, self.versions)

    def order_by(self, field, direction=None):
        return FakeQuery(list(self.store.values())).order_by(field, direction)

    async def stream(self):
        for key, value in self.store.items():
            yield FakeDoc(value, self.document(key), key)


class FakeDb:
    def __init__(self):
        self.collections = {}
        self.versions = {}
        self.before_commit = None
        self.fail_commit = False
        self.transaction_commits = 0

    def collection(self, name):
        return FakeCollection(
            self.collections.setdefault(name, {}), self.versions.setdefault(name, {})
        )

    def transaction(self):
        db = self

        class Transaction:
            # Exercise the actual SDK's async_transactional retry wrapper.
            _read_only = False
            _max_attempts = 3
            _id = b"test-transaction"

            def _clean_up(self):
                self.reads, self.writes = [], []

            async def _begin(self, retry_id=None):
                pass

            def set(self, ref, data):
                self.writes.append((ref, deepcopy(data)))

            async def _commit(self):
                if db.before_commit is not None:
                    callback, db.before_commit = db.before_commit, None
                    await callback()
                if db.fail_commit:
                    raise RuntimeError("Firestore commit failed")
                if any(ref.versions.get(ref.key, 0) != version for ref, version in self.reads):
                    raise Aborted("concurrent write")
                assert len(self.writes) == 2
                for ref, data in self.writes:
                    await ref.set(data)
                db.transaction_commits += 1

            async def _rollback(self):
                self._clean_up()

        return Transaction()

    def batch(self):
        class Batch:
            def __init__(self):
                self.operations = []

            def update(self, ref, data):
                self.operations.append(lambda: ref.update(data))

            def set(self, ref, data, merge=False):
                self.operations.append(lambda: ref.set(data, merge=merge))

            def create(self, ref, data):
                assert ref.key not in ref.store
                self.operations.append(lambda: ref.set(data))

            async def commit(self):
                for operation in self.operations:
                    await operation()

        return Batch()


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
    # An absent legacy flag must remain distinguishable from an explicit restore.
    assert "dismissed" not in summary
    assert history.summarise("abc", {**RESULT, "dismissed": False}, at)["dismissed"] is False


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
async def test_regeneration_replaces_both_views_without_creating_documents_and_keeps_user_facts(db):
    review_id = await history.save(RESULT)
    await history.set_dismissed(review_id, False)
    await history.set_reporting_status(review_id, "opened", "USER 1")
    ref = db.collection(history.RECORDS).document(review_id)
    await ref.update(
        {
            "case.vehicle.model": "Civic",
            "case.incident.location": "User location",
            "case.edited": ["location"],
            "case.provenance.factsSource": "video_analysis_and_user",
        }
    )
    old = await history.get(review_id)
    old_ids = {name: set(rows) for name, rows in db.collections.items()}
    target = await history.replacement_target(review_id, "gs://test-videos/uploads/new")
    result = {
        **RESULT,
        "video_uri": "gs://test-videos/uploads/new",
        "verdict": "New finding",
        "tracks": [{"vehicleTrackId": "v1", "frames": [{"atSeconds": 1}]}],
        "thumbnail_uri": "gs://test-videos/thumbnails/new.jpg",
    }
    returned = await history.replace(target, result)
    assert returned == await history.get(review_id)
    assert {name: set(rows) for name, rows in db.collections.items()} == old_ids
    assert db.transaction_commits == 1
    assert returned["review_id"] == review_id
    assert returned["created_at"] == old["created_at"]
    assert returned["case"]["id"] == old["case"]["id"]
    assert returned["case"]["version"] == old["case"]["version"] + 1
    assert returned["case"]["vehicle"]["model"] == "Civic"
    assert returned["case"]["vehicle"]["plate"] == "USER 1"
    assert returned["case"]["incident"]["location"] == "User location"
    assert returned["case"]["incident"]["description"] == "New finding"
    assert returned["case"]["reporting"] == old["case"]["reporting"]
    assert returned["case"]["dismissed"] is False
    assert returned["case"]["evidence"]["tracks"] == result["tracks"]
    assert returned["decision_trail"]["verdict"] == "New finding"
    assert returned["regenerated_at"] > old["created_at"]
    summary = db.collections[history.SUMMARIES][review_id]
    assert summary["video_uri"] == result["video_uri"]
    assert summary["thumbnail_uri"] == result["thumbnail_uri"]
    assert summary["plate"] == "USER 1"
    assert summary["dismissed"] is False


@pytest.mark.anyio
async def test_legacy_regeneration_keeps_the_case_id_that_the_mobile_mapper_already_shows(db):
    at = datetime.now(timezone.utc)
    review_id = "a" * 32
    db.collections[history.RECORDS] = {
        review_id: {**RESULT, "created_at": at, "review_id": review_id}
    }
    db.collections[history.SUMMARIES] = {
        review_id: {"review_id": review_id, "created_at": at, "dismissed": False}
    }
    target = await history.replacement_target(review_id, "gs://test-videos/uploads/new")
    out = await history.replace(target, {**RESULT, "video_uri": "gs://test-videos/uploads/new"})
    assert out["case"]["id"] == f"cloud:{review_id}-1"
    assert out["decision_trail"]["caseId"] == out["case"]["id"]
    assert out["created_at"] == at
    assert out["case"]["reporting"]["status"] == "ready"
    assert db.collections[history.SUMMARIES][review_id]["dismissed"] is False
    assert len(db.collections[history.RECORDS]) == len(db.collections[history.SUMMARIES]) == 1


@pytest.mark.anyio
async def test_a_fresh_clean_verdict_clears_an_old_opened_draft_without_adding_a_record(db):
    review_id = await history.save(RESULT)
    await history.set_reporting_status(review_id, "opened")
    target = await history.replacement_target(review_id, "gs://test-videos/uploads/new")
    out = await history.replace(
        target,
        {
            **RESULT,
            "video_uri": "gs://test-videos/uploads/new",
            "reportable_count": 0,
            "reportable": [],
        },
    )
    assert out["case"]["reporting"]["status"] == "clean"
    assert out["case"]["offences"] == []
    assert db.collections[history.SUMMARIES][review_id]["reporting_status"] == "clean"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "collection,field,value",
    [
        (history.RECORDS, "dismissed", True),
        (history.RECORDS, "case.dismissed", True),
        (history.SUMMARIES, "dismissed", True),
        (history.RECORDS, "case.reporting.status", "sent"),
        (history.SUMMARIES, "reporting_status", "sent"),
    ],
)
async def test_regeneration_rejects_dismissed_or_sent_in_either_view(db, collection, field, value):
    review_id = await history.save(RESULT)
    await db.collection(collection).document(review_id).update({field: value})
    with pytest.raises(history.ReplacementConflict):
        await history.replacement_target(review_id, "gs://test-videos/uploads/new")
    assert db.transaction_commits == 0


@pytest.mark.anyio
async def test_regeneration_refuses_the_same_upload_before_any_evidence_can_be_overwritten(db):
    review_id = await history.save(RESULT)
    with pytest.raises(history.ReplacementConflict, match="fresh upload"):
        await history.replacement_target(review_id, RESULT["video_uri"])
    with pytest.raises(history.ReplacementNotFound):
        await history.replacement_target("missing", "gs://test-videos/uploads/new")


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["sent", "dismissed", "record_edit", "summary_edit", "delete"])
async def test_a_change_during_analysis_is_preserved_and_replacement_is_refused(db, change):
    review_id = await history.save(RESULT)
    target = await history.replacement_target(review_id, "gs://test-videos/uploads/new")
    if change == "sent":
        await history.set_reporting_status(review_id, "sent")
    elif change == "dismissed":
        await history.set_dismissed(review_id, True)
    elif change == "delete":
        await history.delete(review_id)
    elif change == "record_edit":
        await (
            db.collection(history.RECORDS).document(review_id).update({"case.vehicle.model": "New"})
        )
    else:
        await db.collection(history.SUMMARIES).document(review_id).update({"plate": "USER 1"})
    changed = deepcopy(db.collections)
    with pytest.raises(history.ReplacementConflict):
        await history.replace(target, {**RESULT, "video_uri": "gs://test-videos/uploads/new"})
    assert db.collections == changed
    assert db.transaction_commits == 0


@pytest.mark.anyio
async def test_sdk_transaction_retry_does_not_overwrite_a_report_sent_during_commit(db):
    review_id = await history.save(RESULT)
    target = await history.replacement_target(review_id, "gs://test-videos/uploads/new")

    async def send_before_commit():
        await history.set_reporting_status(review_id, "sent")

    db.before_commit = send_before_commit
    with pytest.raises(history.ReplacementConflict, match="Sent"):
        await history.replace(target, {**RESULT, "video_uri": "gs://test-videos/uploads/new"})
    stored = await history.get(review_id)
    assert stored["video_uri"] == RESULT["video_uri"]
    assert stored["case"]["reporting"]["status"] == "sent"
    assert db.collections[history.SUMMARIES][review_id]["reporting_status"] == "sent"
    assert db.transaction_commits == 0


@pytest.mark.anyio
async def test_two_simultaneous_regenerations_cannot_overwrite_each_other(db):
    review_id = await history.save(RESULT)
    first = await history.replacement_target(review_id, "gs://test-videos/uploads/first")
    second = await history.replacement_target(review_id, "gs://test-videos/uploads/second")
    await history.replace(first, {**RESULT, "video_uri": "gs://test-videos/uploads/first"})
    committed = deepcopy(db.collections)
    with pytest.raises(history.ReplacementConflict, match="changed"):
        await history.replace(second, {**RESULT, "video_uri": "gs://test-videos/uploads/second"})
    assert db.collections == committed
    assert db.transaction_commits == 1


@pytest.mark.anyio
async def test_failed_replacement_commit_raises_and_preserves_both_old_views(db):
    review_id = await history.save(RESULT)
    target = await history.replacement_target(review_id, "gs://test-videos/uploads/new")
    before = deepcopy(db.collections)
    db.fail_commit = True
    with pytest.raises(RuntimeError, match="commit failed"):
        await history.replace(target, {**RESULT, "video_uri": "gs://test-videos/uploads/new"})
    assert db.collections == before
    assert db.transaction_commits == 0


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
        "thumbnail_uri": "gs://b/thumbnails/a.jpg",
        "case": {"id": "RR-1", "evidence": {"annotatedVideoUrl": None, "tracks": []}},
        "decision_trail": {"caseId": "RR-1", "excerptVideoUrl": None},
    }
    out = history.with_playback(record)
    assert out["case"]["evidence"]["annotatedVideoUrl"] == "https://signed/gs://b/annotated/a"
    assert out["case"]["evidence"]["originalVideoUrl"] == "https://signed/gs://b/uploads/a"
    assert out["decision_trail"]["excerptVideoUrl"] == "https://signed/gs://b/excerpt/a"
    assert out["case"]["evidence"]["thumbnailUrl"] == "https://signed/gs://b/thumbnails/a.jpg"
    assert out["thumbnail_url"] == "https://signed/gs://b/thumbnails/a.jpg"
    assert "thumbnailUrl" not in record["case"]["evidence"]
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


def test_separate_sent_cases_with_the_same_plate_each_count_as_a_report():
    out = history.tally([summary("AAA 1", "sent"), summary("AAA 1", "sent")], "2026-10-08")
    assert out["snitchedCount"] == 2


def test_sightings_count_every_case_not_distinct_plates():
    # The same plate in three cases is three sightings.
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


def test_report_total_matches_non_dismissed_sent_cases_from_stored_history(client, db):
    db.collections[history.SUMMARIES] = {
        "same-plate-first": summary("AAA 1", "sent"),
        "same-plate-second": {**summary("AAA 1", "sent"), "dismissed": False},
        "unknown-plate": summary(None, "sent"),
        "dismissed-sent": {**summary("AAA 1", "sent"), "dismissed": True},
        "ready": summary("BBB 2", "ready"),
        "opened": summary("CCC 3", "opened"),
        "legacy": summary("DDD 4", None),
    }
    response = client.get("/v1/impact")
    assert response.status_code == 200
    assert response.json()["snitchedCount"] == 3


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


@pytest.mark.anyio
async def test_dismissal_preserves_reporting_verdict_and_evidence_and_is_reversible(db):
    review_id = await history.save({**RESULT, "thumbnail_uri": "gs://b/thumbnails/a.jpg"})
    await history.set_reporting_status(review_id, "sent")
    assert await history.set_dismissed(review_id, True)
    record = await history.get(review_id)
    assert record["dismissed"] is True
    assert record["case"]["dismissed"] is True
    assert record["case"]["reporting"]["status"] == "sent"
    assert record["verdict"] == RESULT["verdict"]
    assert record["thumbnail_uri"] == "gs://b/thumbnails/a.jpg"
    assert db.collections[history.SUMMARIES][review_id]["dismissed"] is True
    assert await history.set_dismissed(review_id, False)
    assert (await history.get(review_id))["case"]["dismissed"] is False
    assert not await history.set_dismissed("missing", True)


def test_dismissal_api_validates_boolean_and_reports_unknown_ids(client, db):
    assert (
        client.patch("/v1/reviews/missing/dismissal", json={"dismissed": True}).status_code == 404
    )
    for invalid in [{}, {"dismissed": "true"}, {"dismissed": True, "status": "sent"}]:
        assert client.patch("/v1/reviews/missing/dismissal", json=invalid).status_code == 422


@pytest.mark.anyio
async def test_dismissal_of_legacy_review_does_not_create_an_incomplete_case(db):
    db.collections[history.RECORDS] = {"old": {**RESULT, "created_at": datetime.now(timezone.utc)}}
    assert await history.set_dismissed("old", True)
    record = await history.get("old")
    assert record["dismissed"] is True and "case" not in record
    assert db.collections[history.SUMMARIES]["old"]["created_at"] == record["created_at"]


def test_dismissed_cases_do_not_contribute_to_any_impact_counter():
    dismissed = {**summary("HIDDEN 1", "sent", ["speed"]), "dismissed": True}
    visible = summary("VISIBLE 1", "sent", ["lane"])
    assert history.tally([dismissed, visible], "2026-10-18") == history.tally(
        [visible], "2026-10-18"
    )


@pytest.mark.anyio
async def test_thumbnail_backfill_preserves_all_other_fields_and_can_be_repeated(db, monkeypatch):
    from app import thumbnail_backfill

    review_id = await history.save(RESULT)
    await history.set_reporting_status(review_id, "sent")
    await history.set_dismissed(review_id, True)
    before = deepcopy(db.collections)
    render = Mock(return_value="gs://test-videos/thumbnails/preview.jpg")
    monkeypatch.setattr(thumbnail_backfill.thumbnails, "render", render)
    assert (await thumbnail_backfill.backfill())["pending"] == 1
    render.assert_not_called()
    assert db.collections == before
    assert (await thumbnail_backfill.backfill(apply=True))["updated"] == 1
    after = deepcopy(db.collections)
    for collection in [history.RECORDS, history.SUMMARIES]:
        assert after[collection][review_id].pop("thumbnail_uri") == render.return_value
        before[collection][review_id].pop("thumbnail_uri", None)
    assert after == before
    result = await thumbnail_backfill.backfill(apply=True)
    assert result["updated"] == 0 and result["existing"] == 1 and result["failed"] == []
