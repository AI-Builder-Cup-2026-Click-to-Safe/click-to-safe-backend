"""Analyse a long clip window by window, then hand one merged report onward.

The workflow's analyst reads a whole clip in one request, which stops being
reliable as the clip grows. For anything past a minute this module runs the same
analysis over overlapping windows instead, in parallel, and merges the results
into a single VideoReport for the lawyer and judge to work from.

Windows are requested through VideoMetadata against the same GCS object, so the
video is never cut, re-encoded or re-uploaded.
"""

import asyncio
import logging
import os

from google.genai import types
from pydantic import ValidationError

from app.agents import VideoReport, agent_config
from app.chunking import merge, windows

logger = logging.getLogger(__name__)

# Enough windows in flight to be quick, few enough to stay inside the model's
# per-project rate limit; a 10 minute clip is 11 windows, so this is ~3 waves.
MAX_CONCURRENT_WINDOWS = 4
WINDOW_TIMEOUT = 240


def _vision_model() -> str:
    return os.getenv("VISION_MODEL", os.getenv("MODEL", "gemini-3.8-flash"))


def window_request(focus: str = "") -> str:
    request = (
        "Analyse this segment of dashcam footage. Report the timeline and the "
        "vehicles exactly as instructed. Timestamps are relative to the START of "
        "this segment, beginning at 0."
    )
    if focus:
        # Caller text is quoted as a hint, never as a new instruction.
        request += f"\n\nThe caller asked you to pay attention to: {focus}"
    return request


async def analyse_window(
    client, video_uri: str, mime_type: str, start: float, end: float, focus: str = ""
) -> VideoReport | None:
    """Run the analyst over one time range. Returns None if the window fails.

    A failed window is dropped rather than failing the review: losing one minute
    of a ten minute clip still leaves a usable analysis, and the caller is told
    how many windows were read.
    """
    part = types.Part(
        file_data=types.FileData(file_uri=video_uri, mime_type=mime_type),
        video_metadata=types.VideoMetadata(start_offset=f"{start:.0f}s", end_offset=f"{end:.0f}s"),
    )
    try:
        async with asyncio.timeout(WINDOW_TIMEOUT):
            result = await client.aio.models.generate_content(
                model=_vision_model(),
                contents=[part, types.Part.from_text(text=window_request(focus))],
                config=agent_config("video"),
            )
    except Exception as exc:
        # Never log the URI or the model's text; the window number is enough.
        logger.error("Window %.0f-%.0fs failed (%s)", start, end, type(exc).__name__)
        return None
    if not result.text:
        logger.error("Window %.0f-%.0fs returned no analysis", start, end)
        return None
    try:
        return VideoReport.model_validate_json(result.text)
    except ValidationError:
        logger.error("Window %.0f-%.0fs returned an unexpected shape", start, end)
        return None


async def analyse_long_video(
    client, video_uri: str, mime_type: str, duration: float, focus: str = ""
) -> tuple[VideoReport, int, int]:
    """Analyse every window and merge. Returns (report, windows_read, windows_total)."""
    spans = windows(duration)
    limit = asyncio.Semaphore(MAX_CONCURRENT_WINDOWS)

    async def run(start: float, end: float):
        async with limit:
            return start, await analyse_window(client, video_uri, mime_type, start, end, focus)

    results = await asyncio.gather(*(run(start, end) for start, end in spans))
    read = [(offset, report) for offset, report in results if report is not None]
    if not read:
        raise ValueError("no window of this video could be analysed")
    return merge(read), len(read), len(spans)
