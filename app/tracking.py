"""Locate a reported vehicle in the footage, frame by frame.

Once the judge names a vehicle and the window it offended in, a further pass
asks the model where that vehicle sits on screen across that window. The answer
is what lets a player draw a box over the car instead of describing it in prose.

Only reported vehicles are tracked, and only across the seconds the judge cited,
so this costs a few seconds of video per case rather than another pass over the
clip.

Gemini returns boxes as [y_min, x_min, y_max, x_max] normalised to 0-1000. The
front end's contract wants {atSeconds, x, y, width, height} normalised to 0-1,
so the conversion happens here, once, at the boundary.
"""

import asyncio
import logging
import os

from google.genai import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

logger = logging.getLogger(__name__)


def _tracking_model() -> str:
    return os.getenv("VISION_MODEL", os.getenv("MODEL", "gemini-3.8-flash"))


# One box a second reads as smooth motion in a player without asking the model
# for more points than it can place accurately.
SAMPLES_PER_SECOND = 1
MAX_SAMPLES = 30
TRACK_TIMEOUT = 180
GEMINI_SCALE = 1000.0

TRACK_INSTRUCTION = (
    "You locate one specific vehicle in dashcam footage.\n"
    "You are given a segment and a description of the vehicle to find.\n"
    "Rules:\n"
    "- Report the vehicle's position once per second of the segment.\n"
    "- box_2d is [y_min, x_min, y_max, x_max], normalised 0-1000, with the "
    "origin at the top left.\n"
    "- at_seconds is the time WITHIN this segment, starting at 0.\n"
    "- Report only the vehicle described. Do not report other vehicles.\n"
    "- Omit any second where the vehicle is not visible rather than guessing a "
    "position for it."
)

TRACK_SCHEMA = {
    "type": "object",
    "properties": {
        "frames": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "at_seconds": {"type": "number"},
                    "box_2d": {
                        "type": "array",
                        "items": {"type": "number"},
                        "minItems": 4,
                        "maxItems": 4,
                    },
                },
                "required": ["at_seconds", "box_2d"],
            },
        },
    },
    "required": ["frames"],
}


class RawFrame(BaseModel):
    model_config = ConfigDict(extra="ignore")
    at_seconds: float = Field(ge=0)
    box_2d: list[float]


class RawTrack(BaseModel):
    model_config = ConfigDict(extra="ignore")
    frames: list[RawFrame] = Field(default_factory=list)


def to_track_frame(frame: RawFrame, offset: float) -> dict | None:
    """One Gemini box as the front end's TrackFrame, or None if it is unusable.

    The model occasionally returns a degenerate or inverted box; a zero-area or
    out-of-range box would draw as a dot or disappear, so it is dropped rather
    than rendered.
    """
    if len(frame.box_2d) != 4:
        return None
    y_min, x_min, y_max, x_max = (v / GEMINI_SCALE for v in frame.box_2d)
    width, height = x_max - x_min, y_max - y_min
    if width <= 0 or height <= 0:
        return None
    if not (0 <= x_min <= 1 and 0 <= y_min <= 1 and x_max <= 1 and y_max <= 1):
        return None
    return {
        # Back onto the clip's own timeline: the model counted from the segment.
        "atSeconds": round(offset + frame.at_seconds, 2),
        "x": round(x_min, 4),
        "y": round(y_min, 4),
        "width": round(width, 4),
        "height": round(height, 4),
    }


def track_request(vehicle: dict) -> str:
    appearance = vehicle.get("appearance") or vehicle.get("vehicle_id", "the vehicle")
    conduct = vehicle.get("conduct", "")
    request = f"Find this vehicle in the segment: {appearance}."
    if conduct:
        # What it was doing disambiguates it from similar cars in frame.
        request += f" It is the vehicle that: {conduct}"
    return request


async def track_vehicle(client, video_uri: str, mime_type: str, vehicle: dict) -> dict | None:
    """Box one reported vehicle across the seconds it was reported for.

    Returns a track in the front end's shape, or None when the vehicle could not
    be located — a case without a track is still a valid case.
    """
    start = float(vehicle.get("start_seconds", 0))
    end = float(vehicle.get("end_seconds", 0))
    if end <= start:
        return None
    # Cap the span so one long citation cannot turn into a huge request.
    end = min(end, start + MAX_SAMPLES / SAMPLES_PER_SECOND)

    part = types.Part(
        file_data=types.FileData(file_uri=video_uri, mime_type=mime_type),
        video_metadata=types.VideoMetadata(start_offset=f"{start:.0f}s", end_offset=f"{end:.0f}s"),
    )
    try:
        result = await asyncio.wait_for(
            client.aio.models.generate_content(
                model=_tracking_model(),
                contents=[part, types.Part.from_text(text=track_request(vehicle))],
                config=types.GenerateContentConfig(
                    system_instruction=TRACK_INSTRUCTION,
                    response_mime_type="application/json",
                    response_schema=TRACK_SCHEMA,
                    max_output_tokens=4096,
                ),
            ),
            timeout=TRACK_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Could not track %s (%s)", vehicle.get("vehicle_id", "?"), type(exc).__name__)
        return None
    if not result.text:
        return None
    try:
        raw = RawTrack.model_validate_json(result.text)
    except ValidationError:
        logger.error("Tracking returned an unexpected shape")
        return None

    frames = [f for f in (to_track_frame(fr, start) for fr in raw.frames) if f]
    if not frames:
        return None
    frames.sort(key=lambda f: f["atSeconds"])
    return {"vehicleTrackId": vehicle.get("vehicle_id", ""), "frames": frames}


async def track_reported(
    client, video_uri: str, mime_type: str, reported: list[dict]
) -> list[dict]:
    """Track every reported vehicle. Sequential, for the reason windows are.

    Reading several ranges of one object at once made the model hang when the
    windows were first written, so this follows the same one-at-a-time rule.
    """
    tracks = []
    for vehicle in reported:
        track = await track_vehicle(client, video_uri, mime_type, vehicle)
        if track:
            tracks.append(track)
    return tracks
