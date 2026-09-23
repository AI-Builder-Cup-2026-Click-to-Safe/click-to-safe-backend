from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app import main

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
    return blob, runner


def test_review_returns_the_judges_decision(client, cloud):
    _, runner = cloud
    data = client.post("/v1/review", json={"video_uri": "gs://test-videos/uploads/a"}).json()
    assert data["reportable_count"] == 1
    assert data["rounds_used"] == 2
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
