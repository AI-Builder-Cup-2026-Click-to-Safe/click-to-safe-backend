import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app import main

REPORT = {
    "violations": [
        {
            "violation_type": "shoulder_driving",
            "description": "Drove on the shoulder",
            "start_seconds": 18.0,
            "end_seconds": 22.0,
            "vehicle": "white SUV",
            "confidence": 0.95,
            "evidence": "Overtook on the left shoulder during congestion",
        },
        {
            "violation_type": "tailgating",
            "description": "Followed too closely",
            "start_seconds": 3.0,
            "end_seconds": 5.0,
            "vehicle": "black sedan",
            "confidence": 0.2,
            "evidence": "Briefly and partially visible",
        },
    ],
    "summary": "A white SUV was observed driving on the shoulder",
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
    generate = AsyncMock(return_value=SimpleNamespace(text=json.dumps(REPORT)))
    model = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(main, "storage_client", lambda: storage)
    monkeypatch.setattr(main, "model_client", lambda: model)
    return blob, generate


def test_low_confidence_findings_are_filtered_out(client, cloud):
    body = {"video_uri": "gs://test-videos/uploads/a", "min_confidence": 0.5}
    data = client.post("/v1/violations", json=body).json()
    assert data["violation_count"] == 1
    assert data["filtered_out"] == 1
    assert data["violations"][0]["violation_type"] == "shoulder_driving"


def test_lowering_the_bar_keeps_uncertain_findings(client, cloud):
    body = {"video_uri": "gs://test-videos/uploads/a", "min_confidence": 0.0}
    data = client.post("/v1/violations", json=body).json()
    assert data["violation_count"] == 2
    # Results are ordered by when they occur, not by model output order.
    assert [v["start_seconds"] for v in data["violations"]] == [3.0, 18.0]


def test_video_is_sent_as_media_with_a_json_schema(client, cloud):
    _, generate = cloud
    uri = "gs://test-videos/uploads/a"
    assert client.post("/v1/violations", json={"video_uri": uri}).status_code == 200
    kwargs = generate.call_args.kwargs
    assert kwargs["contents"][0].file_data.file_uri == uri
    assert kwargs["config"].response_mime_type == "application/json"


def test_foreign_bucket_is_rejected_before_any_cloud_call(client, cloud):
    blob, generate = cloud
    response = client.post("/v1/violations", json={"video_uri": "gs://other/uploads/a"})
    assert response.status_code == 400
    blob.reload.assert_not_called()
    generate.assert_not_called()


def test_clean_footage_reports_no_violations(client, cloud, monkeypatch):
    _, generate = cloud
    generate.return_value = SimpleNamespace(
        text=json.dumps({"violations": [], "summary": "No violations observed"})
    )
    data = client.post("/v1/violations", json={"video_uri": "gs://test-videos/uploads/a"}).json()
    assert data["violation_count"] == 0
    assert data["violations"] == []


def test_unparseable_model_output_is_502_not_a_crash(client, cloud):
    _, generate = cloud
    generate.return_value = SimpleNamespace(text="not json at all")
    response = client.post("/v1/violations", json={"video_uri": "gs://test-videos/uploads/a"})
    assert response.status_code == 502


def test_runtime_contract_exposes_review_violations(client, cloud):
    payload = {
        "class_method": "review_violations",
        "input": {"video_uri": "gs://test-videos/uploads/a"},
    }
    data = client.post("/api/reasoning_engine", json=payload).json()
    assert data["output"]["violation_count"] == 1


def test_confidence_bound_is_validated(client):
    body = {"video_uri": "gs://test-videos/uploads/a", "min_confidence": 1.5}
    assert client.post("/v1/violations", json=body).status_code == 422
