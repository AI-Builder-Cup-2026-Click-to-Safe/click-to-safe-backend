import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from google.api_core.exceptions import NotFound

from app import main


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("VIDEO_BUCKET", "test-videos")
    return TestClient(main.app)


@pytest.fixture
def cloud(monkeypatch):
    blob = Mock(size=1234, content_type="video/mp4")
    blob.create_resumable_upload_session.return_value = (
        "https://storage.googleapis.com/upload-session"
    )
    storage = Mock()
    storage.bucket.return_value.blob.return_value = blob
    generate = AsyncMock(return_value=SimpleNamespace(text="video analysis result"))
    model = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate)))
    monkeypatch.setattr(main, "storage_client", lambda: storage)
    monkeypatch.setattr(main, "model_client", lambda: model)
    return blob, generate


def test_health_without_cloud_credentials(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post("/api/reasoning_engine", json={"class_method": "health"}).json() == {
        "output": {"status": "ok"}
    }


@pytest.mark.parametrize("content_type", ["application/octet-stream", "text/plain"])
def test_runtime_proxy_json_without_json_content_type(client, content_type):
    response = client.post(
        "/api/reasoning_engine",
        content=json.dumps({"class_method": "health", "input": {}}),
        headers={"Content-Type": content_type},
    )
    assert response.status_code == 200
    assert response.json() == {"output": {"status": "ok"}}


def test_runtime_proxy_string_envelope(client):
    response = client.post(
        "/api/reasoning_engine", json=json.dumps({"class_method": "health", "input": {}})
    )
    assert response.status_code == 200


def test_runtime_proxy_rejects_malformed_json(client):
    assert client.post("/api/reasoning_engine", content="not-json").status_code == 422


def test_upload_session_is_single_object_and_size_bound(client, cloud):
    blob, _ = cloud
    response = client.post("/v1/uploads", json={"size_bytes": 1234})
    assert response.status_code == 200
    assert response.json()["video_uri"].startswith("gs://test-videos/uploads/")
    blob.create_resumable_upload_session.assert_called_once_with(
        content_type="video/mp4", size=1234, if_generation_match=0, timeout=30
    )


@pytest.mark.parametrize("size", [0, -1, main.MAX_VIDEO_BYTES + 1])
def test_upload_size_validation(client, size):
    assert client.post("/v1/uploads", json={"size_bytes": size}).status_code == 422


@pytest.mark.parametrize(
    "uri",
    [
        "https://example.com/a.mp4",
        "gs://another-bucket/uploads/a.mp4",
        "gs://test-videos/private/a.mp4",
        "gs://test-videos/uploads/",
        "gs://test-videos/uploads/a.mp4?token=secret",
    ],
)
def test_rejects_foreign_or_invalid_video_before_cloud_call(client, cloud, uri):
    blob, generate = cloud
    response = client.post("/v1/analyze", json={"video_uri": uri})
    assert response.status_code == 400
    blob.reload.assert_not_called()
    generate.assert_not_called()


def test_video_is_passed_as_media_not_just_a_text_url(client, cloud):
    _, generate = cloud
    uri = "gs://test-videos/uploads/video.mp4"
    response = client.post("/v1/analyze", json={"video_uri": uri})
    assert response.status_code == 200
    assert response.json()["analysis"] == "video analysis result"
    media = generate.call_args.kwargs["contents"][0]
    assert media.file_data.file_uri == uri
    assert media.file_data.mime_type == "video/mp4"


def test_unfinished_upload_is_404(client, cloud):
    blob, generate = cloud
    blob.reload.side_effect = NotFound("missing")
    assert (
        client.post("/v1/analyze", json={"video_uri": "gs://test-videos/uploads/a"}).status_code
        == 404
    )
    generate.assert_not_called()


def test_actual_object_size_is_checked(client, cloud):
    blob, generate = cloud
    blob.size = main.MAX_VIDEO_BYTES + 1
    assert (
        client.post("/v1/analyze", json={"video_uri": "gs://test-videos/uploads/a"}).status_code
        == 413
    )
    generate.assert_not_called()


def test_mime_mismatch_is_rejected(client, cloud):
    blob, generate = cloud
    blob.content_type = "text/plain"
    assert (
        client.post("/v1/analyze", json={"video_uri": "gs://test-videos/uploads/a"}).status_code
        == 400
    )
    generate.assert_not_called()


def test_only_registered_methods_are_callable(client):
    response = client.post("/api/reasoning_engine", json={"class_method": "__getattribute__"})
    assert response.status_code == 400


def test_runtime_query_and_stream_contract(client, cloud):
    payload = {"class_method": "query", "input": {"video_uri": "gs://test-videos/uploads/a"}}
    assert client.post("/api/reasoning_engine", json=payload).json()["output"]["analysis"]
    payload["class_method"] = "stream_query"
    response = client.post("/api/stream_reasoning_engine", json=payload)
    assert response.status_code == 200
    assert response.text.endswith("\n")
    assert response.json()["output"]["analysis"]


def test_invalid_runtime_payload_is_422(client):
    response = client.post("/api/reasoning_engine", json={"class_method": "query", "input": {}})
    assert response.status_code == 422


def test_upstream_errors_do_not_leak_sensitive_details(client, cloud):
    _, generate = cloud
    generate.side_effect = RuntimeError("sensitive-upstream-response")
    response = client.post("/v1/analyze", json={"video_uri": "gs://test-videos/uploads/a"})
    assert response.status_code == 502
    assert "sensitive" not in response.text


def test_stream_errors_keep_http_error_status(client, cloud):
    _, generate = cloud
    generate.side_effect = TimeoutError()
    response = client.post(
        "/api/stream_reasoning_engine",
        json={"class_method": "stream_query", "input": {"video_uri": "gs://test-videos/uploads/a"}},
    )
    assert response.status_code == 504
