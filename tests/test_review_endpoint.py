from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app import history, main

VERDICT = {
    "video_uri": "gs://test-videos/uploads/a",
    "reportable_count": 1,
    "reportable": [{"vehicle_id": "v1", "conduct": "Drove on the shoulder"}],
    "verdict": "One vehicle should be reported.",
    "rounds_used": 2,
    "follow_up_exhausted": False,
    "outstanding_questions": {"video": [], "lawyer": []},
    "evidence": {"timeline": [], "vehicles": [], "legal_analysis": None},
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VIDEO_BUCKET", "test-videos")
    return TestClient(main.app)


@pytest.fixture
def cloud(monkeypatch):
    blob = Mock(size=1234, content_type="video/mp4")
    storage = Mock()
    storage.bucket.return_value.blob.return_value = blob
    monkeypatch.setattr(main, "storage_client", lambda: storage)
    runner = AsyncMock(return_value=VERDICT)
    monkeypatch.setattr(main, "run_review", runner)
    monkeypatch.setattr(
        main.thumbnails, "render", Mock(return_value="gs://test-videos/thumbnails/a.jpg")
    )
    return blob, runner


def test_review_returns_the_judges_decision(client, cloud):
    _, runner = cloud
    data = client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/a"}).json()
    assert data["reportable_count"] == 1
    assert data["rounds_used"] == 2
    assert data["thumbnail_uri"] == "gs://test-videos/thumbnails/a.jpg"
    runner.assert_awaited_once()


def test_caller_focus_reaches_the_agents(client, cloud):
    _, runner = cloud
    body = {"video_uri": "gs://test-videos/uploads/a", "focus": "watch the left lane"}
    assert client.post("/v1/review", json=body).status_code == 200
    assert runner.await_args.args[2] == "watch the left lane"


def test_foreign_bucket_is_rejected_before_any_agent_runs(client, cloud):
    blob, runner = cloud
    response = client.post("/v1/review", json={"video_uri": "gs://other/uploads/a"})
    assert response.status_code == 400
    blob.reload.assert_not_called()
    runner.assert_not_awaited()


def test_a_missing_verdict_is_502_not_a_crash(client, cloud):
    _, runner = cloud
    runner.side_effect = ValueError("the judge returned no usable verdict")
    response = client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/a"})
    assert response.status_code == 502


def test_agent_failures_do_not_leak_upstream_detail(client, cloud):
    _, runner = cloud
    runner.side_effect = RuntimeError("sensitive-upstream-response")
    response = client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/a"})
    assert response.status_code == 502
    assert "sensitive" not in response.text


def test_runtime_contract_exposes_review_traffic(client, cloud):
    payload = {
        "class_method": "review_traffic",
        "input": {"video_uri": "gs://test-videos/uploads/a"},
    }
    data = client.post("/api/reasoning_engine", json=payload).json()
    assert data["output"]["reportable_count"] == 1


def test_runtime_contract_rejects_an_empty_review_input(client):
    response = client.post(
        "/api/reasoning_engine", json={"class_method": "review_traffic", "input": {}}
    )
    assert response.status_code == 422


def test_cost_is_returned_and_saved_after_tracking(client, cloud, monkeypatch):
    from google.genai import types

    from app import costs

    _, runner = cloud
    metadata = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=1000, candidates_token_count=100
    )

    async def review(*args, **kwargs):
        costs.record_usage("gemini-3.8-flash", metadata)
        return dict(VERDICT)

    async def track(*args, **kwargs):
        costs.record_usage("gemini-3.8-flash", metadata)
        return []

    runner.side_effect = review
    monkeypatch.setenv("MODEL_REGION", "global")
    monkeypatch.setattr(main, "clip_duration", AsyncMock(return_value=5))
    monkeypatch.setattr(main, "model_client", lambda: None)
    monkeypatch.setattr(main, "track_reported", track)
    monkeypatch.setattr(main.overlay, "render", AsyncMock(return_value=None))
    saved = AsyncMock(return_value="a" * 32)
    monkeypatch.setattr(main.history, "save", saved)
    response = client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/a"})
    assert response.status_code == 200
    value = response.json()["estimated_ai_cost_usd"]
    assert value is not None and value > 0
    assert saved.await_args.args[0]["estimated_ai_cost_usd"] == value
    assert costs.estimate() is None


def test_a_clip_of_a_few_frames_is_refused_before_any_agent_runs(client, cloud, monkeypatch):
    _, runner = cloud
    monkeypatch.setattr(main, "clip_duration", AsyncMock(return_value=0.23))
    response = client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/a"})
    assert response.status_code == 422
    runner.assert_not_called()


@pytest.fixture
def regeneration(monkeypatch, cloud):
    at = datetime.now(timezone.utc)
    target = history.ReplacementTarget("a" * 32, at, at, "gs://test-videos/uploads/old")
    lookup = AsyncMock(return_value=target)
    monkeypatch.setattr(history, "replacement_target", lookup)
    monkeypatch.setattr(main, "clip_duration", AsyncMock(return_value=5))
    monkeypatch.setattr(main, "model_client", lambda: None)
    tracks = [
        {
            "vehicleTrackId": "v1",
            "frames": [{"atSeconds": 1, "x": 0.1, "y": 0.2, "width": 0.2, "height": 0.3}],
        }
    ]
    tracker = AsyncMock(return_value=tracks)
    monkeypatch.setattr(main, "track_reported", tracker)
    renderer = AsyncMock(side_effect=["gs://test-videos/annotated/a", "gs://test-videos/excerpt/a"])
    monkeypatch.setattr(main.overlay, "render", renderer)
    save = AsyncMock()
    replace = AsyncMock(
        return_value={
            "review_id": target.review_id,
            "created_at": at,
            "regenerated_at": at,
            "video_uri": VERDICT["video_uri"],
            "case": {
                "id": "EXISTING-CASE",
                "vehicle": {"plate": "USER 1"},
                "reporting": {"status": "opened"},
                "evidence": {},
            },
            "decision_trail": {"caseId": "EXISTING-CASE", "verdict": "New verdict"},
        }
    )
    monkeypatch.setattr(history, "save", save)
    monkeypatch.setattr(history, "replace", replace)
    monkeypatch.setattr(history, "playable", lambda uri: f"https://playback/{uri}" if uri else None)
    return lookup, tracker, renderer, save, replace


def replacement_request(client):
    return client.post(
        "/v1/review", json={"video_uri": VERDICT["video_uri"], "replace_review_id": "a" * 32}
    )


def test_regeneration_returns_the_committed_case_instead_of_rebuilding_it(client, regeneration):
    lookup, _, _, save, replace = regeneration
    response = replacement_request(client)
    assert response.status_code == 200
    data = response.json()
    assert data["case"]["id"] == "EXISTING-CASE"
    assert data["case"]["vehicle"]["plate"] == "USER 1"
    assert data["case"]["reporting"]["status"] == "opened"
    assert data["decision_trail"]["caseId"] == "EXISTING-CASE"
    assert data["created_at"] and data["regenerated_at"]
    lookup.assert_awaited_once_with("a" * 32, VERDICT["video_uri"])
    replace.assert_awaited_once()
    save.assert_not_awaited()


@pytest.mark.parametrize(
    "error,status",
    [
        (history.ReplacementNotFound("No such review"), 404),
        (history.ReplacementConflict("Sent reviews cannot be regenerated"), 409),
        (RuntimeError("private database failure"), 503),
    ],
)
def test_regeneration_rejects_ineligible_targets_before_model_or_upload_reads(
    client, cloud, regeneration, error, status
):
    lookup, _, _, save, replace = regeneration
    lookup.side_effect = error
    response = replacement_request(client)
    assert response.status_code == status
    assert "private" not in response.text
    cloud[0].reload.assert_not_called()
    cloud[1].assert_not_awaited()
    save.assert_not_awaited()
    replace.assert_not_awaited()


@pytest.mark.parametrize(
    "failure",
    ["track_error", "empty_tracks", "wrong_vehicle", "overlay", "excerpt", "thumbnail", "coverage"],
)
def test_incomplete_regeneration_never_replaces_working_evidence(
    client, cloud, regeneration, monkeypatch, failure
):
    _, tracker, renderer, save, replace = regeneration
    if failure == "track_error":
        tracker.side_effect = RuntimeError("tracking unavailable")
    elif failure == "empty_tracks":
        tracker.return_value = []
    elif failure == "wrong_vehicle":
        tracker.return_value = [{"vehicleTrackId": "unrelated", "frames": [{"atSeconds": 1}]}]
    elif failure == "overlay":
        renderer.side_effect = [None, "gs://test-videos/excerpt/a"]
    elif failure == "excerpt":
        renderer.side_effect = ["gs://test-videos/annotated/a", None]
    elif failure == "thumbnail":
        monkeypatch.setattr(main.thumbnails, "render", Mock(return_value=None))
    elif failure == "coverage":
        monkeypatch.setattr(
            main,
            "prepared_analysis",
            AsyncMock(
                return_value=(
                    None,
                    {
                        "duration_seconds": 65,
                        "windows_analysed": 1,
                        "windows_total": 2,
                    },
                )
            ),
        )
    response = replacement_request(client)
    assert response.status_code == 502
    save.assert_not_awaited()
    replace.assert_not_awaited()


def test_unknown_duration_is_rejected_before_regeneration_spends_model_calls(
    client, cloud, regeneration, monkeypatch
):
    monkeypatch.setattr(main, "clip_duration", AsyncMock(return_value=None))
    assert replacement_request(client).status_code == 502
    cloud[1].assert_not_awaited()


@pytest.mark.parametrize(
    "error,status",
    [
        (history.ReplacementConflict("Review changed during regeneration"), 409),
        (RuntimeError("private Firestore failure"), 503),
    ],
)
def test_failed_replacement_is_not_returned_as_a_success_or_fallback_insert(
    client, regeneration, error, status
):
    _, _, _, save, replace = regeneration
    replace.side_effect = error
    response = replacement_request(client)
    assert response.status_code == status
    assert "private" not in response.text
    save.assert_not_awaited()


def test_a_successful_clean_regeneration_needs_a_thumbnail_but_no_boxes(
    client, cloud, regeneration
):
    _, tracker, renderer, save, replace = regeneration
    cloud[1].return_value = {**VERDICT, "reportable": [], "reportable_count": 0}
    tracker.return_value = []
    renderer.side_effect = None
    renderer.return_value = None
    assert replacement_request(client).status_code == 200
    replace.assert_awaited_once()
    save.assert_not_awaited()
