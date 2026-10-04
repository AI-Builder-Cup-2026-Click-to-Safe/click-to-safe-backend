import pytest

from app.tracking import MAX_SAMPLES, RawFrame, to_track_frame, track_request, track_vehicle


def frame(box, at=0.0):
    return RawFrame(at_seconds=at, box_2d=box)


def test_a_box_converts_to_the_front_ends_shape():
    # Gemini: [y_min, x_min, y_max, x_max] over 0-1000.
    out = to_track_frame(frame([100, 200, 500, 600]), offset=0)
    assert out == {"atSeconds": 0.0, "x": 0.2, "y": 0.1, "width": 0.4, "height": 0.4}


def test_the_offset_puts_a_frame_back_on_the_clips_timeline():
    # The model counts from the segment's start; the clip's time is what matters.
    assert to_track_frame(frame([0, 0, 100, 100], at=3.0), offset=18.0)["atSeconds"] == 21.0


def test_a_full_frame_box_maps_to_the_whole_picture():
    out = to_track_frame(frame([0, 0, 1000, 1000]), offset=0)
    assert (out["x"], out["y"], out["width"], out["height"]) == (0.0, 0.0, 1.0, 1.0)


@pytest.mark.parametrize(
    "box",
    [
        [500, 200, 100, 600],  # inverted vertically
        [100, 600, 500, 200],  # inverted horizontally
        [100, 200, 100, 600],  # zero height
        [100, 200, 500, 200],  # zero width
    ],
)
def test_a_degenerate_box_is_dropped(box):
    # These would draw as a dot or vanish, so they are worse than no box.
    assert to_track_frame(frame(box), offset=0) is None


@pytest.mark.parametrize("box", [[-10, 0, 500, 500], [0, 0, 1200, 500], [0, 0, 500, 1400]])
def test_a_box_outside_the_picture_is_dropped(box):
    assert to_track_frame(frame(box), offset=0) is None


def test_a_malformed_box_is_dropped():
    assert to_track_frame(frame([100, 200, 300]), offset=0) is None


def test_the_request_names_the_vehicle_and_what_it_did():
    text = track_request({"appearance": "white MPV", "conduct": "cut in without indicating"})
    assert "white MPV" in text and "cut in without indicating" in text


def test_the_request_falls_back_to_the_id_when_there_is_no_description():
    assert "white_mpv" in track_request({"vehicle_id": "white_mpv"})


@pytest.mark.anyio
async def test_a_zero_length_citation_is_not_tracked():
    # Nothing to look at, and the model would be asked for an empty range.
    assert await track_vehicle(None, "gs://b/uploads/a", "video/mp4", {"start_seconds": 5}) is None


@pytest.mark.anyio
async def test_a_tracking_failure_leaves_the_case_intact(monkeypatch):
    class Broken:
        class aio:
            class models:
                @staticmethod
                async def generate_content(**kwargs):
                    raise RuntimeError("vertex is down")

    vehicle = {"vehicle_id": "v1", "start_seconds": 1, "end_seconds": 4}
    # A case without a track is still a case; the review must not fail with it.
    assert await track_vehicle(Broken(), "gs://b/uploads/a", "video/mp4", vehicle) is None


@pytest.mark.anyio
async def test_a_long_citation_is_capped(monkeypatch):
    seen = {}

    class Fake:
        class aio:
            class models:
                @staticmethod
                async def generate_content(contents=None, **kwargs):
                    seen["end"] = contents[0].video_metadata.end_offset
                    raise RuntimeError("stop here; the span is what matters")

    await track_vehicle(
        Fake(), "gs://b/uploads/a", "video/mp4", {"start_seconds": 0, "end_seconds": 9999}
    )
    assert seen["end"] == f"{MAX_SAMPLES:.0f}s"


def test_a_tracking_fault_does_not_fail_a_verdict(monkeypatch):
    """Even building the client can fail; the review must still be answered."""
    from unittest.mock import AsyncMock, Mock

    from fastapi.testclient import TestClient

    from app import main

    monkeypatch.setenv("VIDEO_BUCKET", "test-videos")
    blob = Mock(size=1234, content_type="video/mp4")
    storage = Mock()
    storage.bucket.return_value.blob.return_value = blob
    monkeypatch.setattr(main, "storage_client", lambda: storage)
    monkeypatch.setattr(
        main, "run_review", AsyncMock(return_value={"reportable_count": 1, "reportable": []})
    )
    monkeypatch.setattr(main, "history", Mock(save=AsyncMock(return_value="rid")))

    def broken():
        raise KeyError("APP_PROJECT_ID")

    monkeypatch.setattr(main, "model_client", broken)
    response = TestClient(main.app).post(
        "/v1/review", json={"video_uri": "gs://test-videos/uploads/a"}
    )
    assert response.status_code == 200
    assert response.json()["tracks"] == []
