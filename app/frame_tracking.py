"""Ground vehicle boxes in exact decoded frames instead of model-estimated times.

This refinement path consumes a local original and leaves persistence to its
caller. A missing target is an explicit null observation, never a forced box.
"""

import asyncio
import math
from collections.abc import Callable

from google.genai import types
from pydantic import BaseModel, Field

from app import costs
from app.benchmark import record_usage
from app.tracking import RawFrame, _tracking_model, to_track_frame

MAX_FRAMES = 120
CONCURRENT_FRAMES = 4
FRAME_TIMEOUT = 120


class FrameDetection(BaseModel):
    box_2d: list[float] | None = Field(
        description="Visible target bounds [y_min,x_min,y_max,x_max] normalized 0..1000; "
        "null if the target is absent or cannot be identified confidently"
    )
    visible_description: str


def frame_prompt(vehicle: dict) -> str:
    return (
        "Locate ONLY the described target in this exact dashcam still image. Target appearance: "
        f"{vehicle.get('appearance') or vehicle['vehicle_id']}. "
        f"Context from the video: {vehicle.get('conduct') or ''}\n"
        "Do not infer objects outside the frame or return a different vehicle. Draw a tight box "
        "around only the visible portion of this physical target, excluding shadows/reflections. "
        "If the target is absent or cannot be identified confidently, return box_2d=null. "
        "Coordinates [y_min,x_min,y_max,x_max] normalized to 0..1000 relative to the entire "
        "supplied image, origin top-left. Describe the actually visible target briefly. "
        "This is one exact frame; do not predict another moment or make a legal judgement."
    )


def frame_schedule(
    reported: list[dict], duration: float, fps: float = 2
) -> list[tuple[dict, float]]:
    """Bound both cost and timestamps before extracting or sending any images."""
    if not math.isfinite(duration) or duration <= 0 or not 0 < fps <= 2:
        raise ValueError("Invalid frame sampling duration or rate")
    scheduled = []
    for vehicle in reported:
        start, end = float(vehicle.get("start_seconds", 0)), float(vehicle.get("end_seconds", 0))
        if not vehicle.get("vehicle_id") or not (0 <= start < end <= duration):
            raise ValueError("Reported vehicle has an invalid citation window")
        count = math.ceil((end - start) * fps)
        if len(scheduled) + count > MAX_FRAMES:
            raise ValueError(f"Frame refinement exceeds its {MAX_FRAMES}-image limit")
        scheduled.extend((vehicle, round(start + index / fps, 6)) for index in range(count))
    return scheduled


async def extract_frame(path: str, at_seconds: float) -> bytes:
    """ffmpeg applies display rotation and accurately seeks to the requested instant."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-ss",
        f"{at_seconds:.6f}",
        "-i",
        path,
        "-frames:v",
        "1",
        "-q:v",
        "2",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        image, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except BaseException:
        process.kill()
        await process.wait()
        raise
    if process.returncode or not image.startswith(b"\xff\xd8"):
        raise ValueError("Could not decode the requested video frame")
    return image


async def ground_frame(client, image: bytes, vehicle: dict, at_seconds: float) -> dict:
    """The caller owns time; the model returns only image-space geometry or absence."""
    model = _tracking_model()
    result = await asyncio.wait_for(
        client.aio.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image, mime_type="image/jpeg"),
                types.Part.from_text(text=frame_prompt(vehicle)),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=FrameDetection,
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                max_output_tokens=2048,
                temperature=0,
            ),
        ),
        timeout=FRAME_TIMEOUT,
    )
    usage = getattr(result, "usage_metadata", None)
    record_usage("vehicle_frame_tracking", model, usage)
    costs.record_usage(model, usage)
    detection = FrameDetection.model_validate_json(result.text or "")
    frame = None
    if detection.box_2d is not None:
        frame = to_track_frame(RawFrame(at_seconds=at_seconds, box_2d=detection.box_2d), 0)
        if frame is None:
            raise ValueError("Frame detector returned invalid box geometry")
    return {
        "vehicle_id": vehicle["vehicle_id"],
        "at_seconds": at_seconds,
        "frame": frame,
        "visible_description": detection.visible_description,
        "model": model,
        "usage": usage.model_dump(mode="json") if usage else None,
    }


async def refine_tracks(
    client,
    path: str,
    reported: list[dict],
    duration: float,
    fps: float = 2,
    checkpoint: Callable[[dict], None] | None = None,
) -> dict:
    """Return observations and candidate tracks for review; never publish partial failures."""
    schedule = frame_schedule(reported, duration, fps)
    limit = asyncio.Semaphore(CONCURRENT_FRAMES)

    async def one(vehicle, at):
        async with limit:
            try:
                image = await extract_frame(path, at)
                observation = await ground_frame(client, image, vehicle, at)
            except Exception as exc:
                observation = {
                    "vehicle_id": vehicle["vehicle_id"],
                    "at_seconds": at,
                    "error": type(exc).__name__,
                }
            if checkpoint:
                checkpoint(observation)
            return observation

    observations = await asyncio.gather(*(one(vehicle, at) for vehicle, at in schedule))
    tracks = []
    for vehicle_id in dict.fromkeys(vehicle["vehicle_id"] for vehicle in reported):
        frames = {
            item["at_seconds"]: item["frame"]
            for item in observations
            if item["vehicle_id"] == vehicle_id and item.get("frame") is not None
        }
        if frames:
            tracks.append(
                {
                    "vehicleTrackId": vehicle_id,
                    "frames": [frame for _, frame in sorted(frames.items())],
                }
            )
    return {
        "tracks": tracks,
        "observations": observations,
        "errors": [item for item in observations if item.get("error")],
        "sample_interval_seconds": 1 / fps,
        "requested_frames": len(schedule),
    }
