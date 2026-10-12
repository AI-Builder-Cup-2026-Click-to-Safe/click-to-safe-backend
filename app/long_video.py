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
import time
from functools import lru_cache

from google import genai
from google.genai import types
from pydantic import ValidationError

from app import costs
from app.agents import VideoReport, agent_config
from app.benchmark import record_usage
from app.chunking import WINDOW_SECONDS, merge, windows

logger = logging.getLogger(__name__)

# How many windows may be read at once. Each read waits on Gemini rather than
# this instance, so reading in parallel shortens a long review without adding
# tokens: a 30 minute clip spent 283s reading seven windows one at a time.
MAX_CONCURRENT_WINDOWS = int(os.getenv("WINDOW_CONCURRENCY", "4"))
WINDOW_TIMEOUT = 240


def _vision_model() -> str:
    return os.getenv("VISION_MODEL", os.getenv("MODEL", "gemini-3.8-flash"))


@lru_cache
def window_client():
    """A client whose HTTP timeout bounds a window on its own.

    asyncio can only interrupt a call that cooperates with cancellation, so the
    transport's own deadline is what actually stops a window that hangs. It is
    set below WINDOW_TIMEOUT so the HTTP layer gives up first and the error is
    reported against the window rather than swallowing the whole review.
    """
    return genai.Client(
        vertexai=True,
        project=os.environ["APP_PROJECT_ID"],
        location=os.getenv("MODEL_REGION", "global"),
        http_options=types.HttpOptions(api_version="v1", timeout=(WINDOW_TIMEOUT - 30) * 1000),
    )


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
    video_uri: str, mime_type: str, start: float, end: float, focus: str = ""
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
    logger.info("Window %.0f-%.0fs started", start, end)
    began = time.monotonic()
    try:
        # This bounds a call that cooperates with cancellation. A client that
        # ignores it cannot be interrupted here at all, so the real limit is the
        # HTTP timeout on the client itself (see window_client).
        result = await asyncio.wait_for(
            window_client().aio.models.generate_content(
                model=_vision_model(),
                contents=[part, types.Part.from_text(text=window_request(focus))],
                config=agent_config("video"),
            ),
            timeout=WINDOW_TIMEOUT,
        )
    except Exception as exc:
        # Never log the URI or the model's text; the window number is enough.
        logger.error(
            "Window %.0f-%.0fs failed after %.0fs (%s)",
            start,
            end,
            time.monotonic() - began,
            type(exc).__name__,
        )
        return None
    record_usage("video_window", _vision_model(), getattr(result, "usage_metadata", None))
    costs.record_usage(_vision_model(), getattr(result, "usage_metadata", None))
    logger.info("Window %.0f-%.0fs read in %.0fs", start, end, time.monotonic() - began)
    if not result.text:
        logger.error("Window %.0f-%.0fs returned no analysis", start, end)
        return None
    try:
        return VideoReport.model_validate_json(result.text)
    except ValidationError:
        logger.error("Window %.0f-%.0fs returned an unexpected shape", start, end)
        return None


async def analyse_long_video(
    video_uri: str,
    mime_type: str,
    duration: float,
    focus: str = "",
    window: float = WINDOW_SECONDS,
) -> tuple[VideoReport, int, int]:
    """Analyse every window and merge. Returns (report, windows_read, windows_total)."""
    spans = windows(duration, window)
    limit = asyncio.Semaphore(MAX_CONCURRENT_WINDOWS)

    async def run(start: float, end: float):
        async with limit:
            return start, await analyse_window(video_uri, mime_type, start, end, focus)

    started = time.monotonic()
    results = await asyncio.gather(*(run(start, end) for start, end in spans))
    read = [(offset, report) for offset, report in results if report is not None]
    # Timing per stage, or a slow review gives no clue which half was slow.
    logger.info(
        "Analysed %d/%d windows in %.0fs", len(read), len(spans), time.monotonic() - started
    )
    if not read:
        raise ValueError("no window of this video could be analysed")
    return merge(read), len(read), len(spans)
