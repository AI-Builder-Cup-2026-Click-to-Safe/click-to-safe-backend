import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import frame_tracking


def vehicle(start=17.25, end=18.5):
    return {
        "vehicle_id": "blue",
        "appearance": "blue sedan",
        "conduct": "merges",
        "start_seconds": start,
        "end_seconds": end,
    }


def client(answer):
    call = AsyncMock(return_value=SimpleNamespace(text=json.dumps(answer), usage_metadata=None))
    return SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=call))), call


def test_fractional_timestamps_remain_exact_and_sampling_never_exceeds_the_citation():
    result = frame_tracking.frame_schedule([vehicle()], 20)
    assert [at for _, at in result] == [17.25, 17.75, 18.25]


def test_budget_applies_to_all_vehicles_without_silently_dropping_later_ones():
    with pytest.raises(ValueError, match="120-image"):
        frame_tracking.frame_schedule([vehicle(0, 30), vehicle(30, 61)], 100)
    result = frame_tracking.frame_schedule([vehicle(0, 30), vehicle(30, 60)], 100)
    assert len(result) == 120
    assert result[-1][1] == 59.5


@pytest.mark.parametrize(
    "reported,duration,fps",
    [
        ([vehicle(0, 61)], 100, 2),
        ([vehicle(0, 21)], 20, 2),
        ([vehicle(3, 2)], 20, 2),
        ([vehicle()], 20, 3),
        ([vehicle()], float("inf"), 2),
        ([vehicle(0, 1e12)], 1e12, 2),
    ],
)
def test_unbounded_or_invalid_sampling_is_rejected_before_model_calls(reported, duration, fps):
    with pytest.raises(ValueError):
        frame_tracking.frame_schedule(reported, duration, fps)


@pytest.mark.anyio
async def test_box_time_comes_from_the_exact_image_not_from_a_model_timestamp():
    api, call = client(
        {"box_2d": [100, 200, 500, 600], "visible_description": "blue sedan", "at_seconds": 999}
    )
    result = await frame_tracking.ground_frame(api, b"jpeg", vehicle(), 17.25)
    assert result["frame"] == {"atSeconds": 17.25, "x": 0.2, "y": 0.1, "width": 0.4, "height": 0.4}
    parts = call.call_args.kwargs["contents"]
    assert parts[0].inline_data.data == b"jpeg"
    assert parts[0].inline_data.mime_type == "image/jpeg"


@pytest.mark.anyio
async def test_an_absent_or_ambiguous_target_stays_absent():
    api, _ = client({"box_2d": None, "visible_description": "not identifiable"})
    result = await frame_tracking.ground_frame(api, b"jpeg", vehicle(), 17.25)
    assert result["frame"] is None


@pytest.mark.anyio
async def test_invalid_geometry_fails_instead_of_becoming_a_visible_box():
    api, _ = client({"box_2d": [600, 200, 100, 500], "visible_description": "wrong"})
    with pytest.raises(ValueError, match="geometry"):
        await frame_tracking.ground_frame(api, b"jpeg", vehicle(), 17.25)


@pytest.mark.anyio
async def test_refinement_records_absence_and_failures_without_forcing_or_interpolating_boxes(
    monkeypatch,
):
    monkeypatch.setattr(frame_tracking, "extract_frame", AsyncMock(return_value=b"jpeg"))

    async def detect(api, image, target, at):
        if at == 17.75:
            return {"vehicle_id": "blue", "at_seconds": at, "frame": None}
        if at == 18.25:
            raise RuntimeError("private response")
        return {
            "vehicle_id": "blue",
            "at_seconds": at,
            "frame": {"atSeconds": at, "x": 0.1, "y": 0.2, "width": 0.1, "height": 0.1},
        }

    monkeypatch.setattr(frame_tracking, "ground_frame", detect)
    saved = []
    result = await frame_tracking.refine_tracks(
        None, "original", [vehicle()], 20, checkpoint=saved.append
    )
    assert len(saved) == result["requested_frames"] == 3
    assert len(result["tracks"][0]["frames"]) == 1
    assert result["errors"] == [
        {"vehicle_id": "blue", "at_seconds": 18.25, "error": "RuntimeError"}
    ]
    assert result["sample_interval_seconds"] == 0.5
