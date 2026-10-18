"""Draw the tracked boxes onto a copy of the clip.

The tracks alone let a player draw boxes live, but a reviewer who wants to send
the evidence on needs a file that carries them. This renders one: the original
video with a box over each reported vehicle for the seconds it was cited.

Boxes arrive one a second. ffmpeg's drawbox takes a single rectangle with an
enable expression, so each box is drawn for the second it belongs to and the
next takes over — the box steps once a second rather than sliding, which is what
the sampling actually supports.
"""

import asyncio
import json
import logging
import math
import os
import shutil
import tempfile

from google.cloud import storage

logger = logging.getLogger(__name__)

RENDER_TIMEOUT = 600
# Reported vehicles get their own colour so two boxes in frame stay apart.
COLOURS = ("red", "yellow", "lime", "cyan", "magenta")
BOX_THICKNESS = 4
# A track's last box is held this long, so the final second does not blink out.
HOLD_SECONDS = 1.0
# Shorter than a frame at 60fps, so no frame shows two boxes for one vehicle.
BOUNDARY_GAP = 0.01
# Seconds kept either side of the boxes in the excerpt, so the manoeuvre has a
# visible run-up and does not end on the frame the last box sits in.
CLIP_PADDING = 3.0


def available() -> bool:
    """Whether this image can render at all."""
    return shutil.which("ffmpeg") is not None


def drawbox_filters(tracks: list[dict], width: int, height: int) -> list[str]:
    """One drawbox per sampled second, enabled only for that second.

    Coordinates are normalised 0-1 so they scale to the clip's real pixel size;
    ffmpeg needs them in pixels.
    """
    filters = []
    for index, track in enumerate(tracks):
        colour = COLOURS[index % len(COLOURS)]
        frames = track.get("frames") or []
        for position, frame in enumerate(frames):
            start = frame["atSeconds"]
            # Hold until just before the next sample. `between` includes both
            # ends, so ending exactly on the next start draws both boxes on that
            # frame and the vehicle appears to have two outlines.
            end = (
                frames[position + 1]["atSeconds"] - BOUNDARY_GAP
                if position + 1 < len(frames)
                else start + HOLD_SECONDS
            )
            x = round(frame["x"] * width)
            y = round(frame["y"] * height)
            w = max(1, round(frame["width"] * width))
            h = max(1, round(frame["height"] * height))
            filters.append(
                f"drawbox=x={x}:y={y}:w={w}:h={h}:color={colour}@1:t={BOX_THICKNESS}"
                f":enable='between(t,{start:.2f},{end:.2f})'"
            )
    return filters


def annotated_name(object_name: str, kind: str = "annotated") -> str:
    """Where a rendered copy lives: beside the uploads, never over them."""
    return f"{kind}/{object_name.removeprefix('uploads/')}"


def cited_span(tracks: list[dict]) -> tuple[float, float] | None:
    """The stretch the boxes cover, with a little room either side.

    A reviewer sending evidence on wants the offence, not the whole drive, and
    the lead-in is what makes the manoeuvre legible: a car appearing already
    halfway across a lane shows nothing.
    """
    times = [f["atSeconds"] for t in tracks for f in (t.get("frames") or [])]
    if not times:
        return None
    return max(0.0, min(times) - CLIP_PADDING), max(times) + CLIP_PADDING + HOLD_SECONDS


async def dimensions(path: str) -> tuple[int, int] | None:
    """Displayed size after ffmpeg's autorotation, matching the model's frame."""
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    try:
        stream = json.loads(out)["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        rotation = next(
            (item["rotation"] for item in stream.get("side_data_list", []) if "rotation" in item),
            (stream.get("tags") or {}).get("rotate", 0),
        )
        # ffmpeg applies the display matrix before drawbox. Phone recordings
        # commonly store landscape pixels with a quarter-turn display rotation.
        if math.isclose(abs(float(rotation)) % 180, 90, abs_tol=0.01):
            width, height = height, width
    except (ValueError, TypeError, KeyError, IndexError):
        logger.error("Could not read the clip's dimensions")
        return None
    return (width, height) if width > 0 and height > 0 else None


def shifted(tracks: list[dict], offset: float) -> list[dict]:
    """The same boxes against a clip that starts `offset` seconds in.

    Box times are measured from the start of the original; once the video is
    trimmed, every one of them must move back by the same amount or the boxes
    draw late — or never, past the end of a short excerpt.
    """
    return [
        {
            **track,
            "frames": [
                {**f, "atSeconds": round(f["atSeconds"] - offset, 2)}
                for f in (track.get("frames") or [])
            ],
        }
        for track in tracks
    ]


async def render(
    video_uri: str, tracks: list[dict], span: tuple[float, float] | None = None
) -> str | None:
    """Render an annotated copy beside the original. Returns its gs:// URI.

    With `span`, only those seconds are kept, and the boxes move with the cut.
    Returns None when there is nothing to draw or the render fails: the file is
    a convenience, and a case keeps its verdict and its tracks without one.
    """
    if not any(track.get("frames") for track in tracks):
        return None
    if not available():
        logger.error("ffmpeg is not installed; skipping the annotated video")
        return None

    kind = "annotated"
    if span:
        start, end = span
        tracks = shifted(tracks, start)
        kind = "excerpt"

    bucket_name, _, object_name = video_uri.removeprefix("gs://").partition("/")
    target_name = annotated_name(object_name, kind)
    bucket = storage.Client(project=os.environ["APP_PROJECT_ID"]).bucket(bucket_name)

    with tempfile.TemporaryDirectory() as work:
        source = os.path.join(work, "source.mp4")
        target = os.path.join(work, "annotated.mp4")
        try:
            await asyncio.to_thread(bucket.blob(object_name).download_to_filename, source)
        except Exception as exc:
            logger.error("Could not fetch the clip to annotate (%s)", type(exc).__name__)
            return None

        size = await dimensions(source)
        if size is None:
            return None
        filters = drawbox_filters(tracks, *size)
        if not filters:
            return None

        # Arguments are passed as a list, never through a shell, so the filter
        # string cannot be read as anything but one argument.
        # -ss before -i seeks fast; -t then bounds the excerpt's length.
        trim = ["-ss", f"{span[0]:.2f}", "-t", f"{span[1] - span[0]:.2f}"] if span else []
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-loglevel",
            "error",
            *trim,
            "-i",
            source,
            "-vf",
            ",".join(filters),
            # Re-encode the picture, copy the sound: the audio is untouched.
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "26",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            target,
            "-y",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=RENDER_TIMEOUT)
        except TimeoutError:
            process.kill()
            logger.error("Annotating the clip timed out")
            return None
        if process.returncode != 0:
            # ffmpeg's own message names the filter that failed; keep it.
            logger.error("ffmpeg failed: %s", (stderr or b"").decode()[:300])
            return None

        try:
            await asyncio.to_thread(
                bucket.blob(target_name).upload_from_filename, target, content_type="video/mp4"
            )
        except Exception as exc:
            logger.error("Could not store the annotated clip (%s)", type(exc).__name__)
            return None

    logger.info("%s written with %d boxes", "Excerpt" if span else "Annotated clip", len(filters))
    return f"gs://{bucket_name}/{target_name}"
