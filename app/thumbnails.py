"""Create small, reusable evidence images in the video's private GCS bucket."""

import hashlib
import math
import os
import subprocess
import tempfile
from pathlib import Path

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

RENDER_TIMEOUT = 90


def frame_seconds(record: dict) -> float:
    """Use this case's first cited moment, including clean and older reviews."""
    offences = (record.get("case") or {}).get("offences") or []
    included = [offence for offence in offences if offence.get("included", True)]
    findings = record.get("reportable") or []
    value = (
        (included or offences)[0].get("atSeconds", 1)
        if offences
        else findings[0].get("start_seconds", 1)
        if findings
        else 1
    )
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            seconds = 1
    except (TypeError, ValueError):
        seconds = 1
    duration = (record.get("coverage") or {}).get("duration_seconds")
    duration = duration or ((record.get("case") or {}).get("evidence") or {}).get("durationSeconds")
    if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
        seconds = min(seconds, max(0, duration - 0.1))
    return round(max(0, seconds), 3)


def image_name(source_uri: str, generation: str | int, seconds: float) -> str:
    identity = f"{source_uri}\n{generation}\n{seconds:.3f}"
    return f"thumbnails/{hashlib.sha256(identity.encode()).hexdigest()}.jpg"


def extract_frame(source: str, target: str, seconds: float) -> float:
    """Seek before decoding, with a first-frame fallback for old invalid timestamps."""
    for position in dict.fromkeys([seconds, 0.0]):
        result = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                f"{position:.3f}",
                "-i",
                source,
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-2",
                "-q:v",
                "4",
                "-an",
                "-y",
                target,
            ],
            capture_output=True,
            timeout=RENDER_TIMEOUT,
            check=False,
        )
        if result.returncode == 0 and Path(target).is_file() and Path(target).stat().st_size:
            return position
    raise RuntimeError("Could not extract a thumbnail from the video")


def render(record: dict) -> str:
    """Return a GCS JPEG URI only after it exists; repeat calls reuse the same image."""
    client = storage.Client(project=os.environ["APP_PROJECT_ID"])
    seconds = frame_seconds(record)
    candidates = list(
        dict.fromkeys(
            filter(
                None,
                [
                    record.get("annotated_video_uri"),
                    record.get("video_uri"),
                ],
            )
        )
    )
    for source_uri in candidates:
        if not source_uri.startswith("gs://"):
            continue
        bucket_name, _, object_name = source_uri.removeprefix("gs://").partition("/")
        if not bucket_name or not object_name:
            continue
        bucket = client.bucket(bucket_name)
        source_blob = bucket.blob(object_name)
        try:
            source_blob.reload(timeout=30)
        except NotFound:
            continue
        target_name = image_name(source_uri, source_blob.generation, seconds)
        target_blob = bucket.blob(target_name)
        if target_blob.exists(timeout=30):
            return f"gs://{bucket_name}/{target_name}"
        with tempfile.TemporaryDirectory() as work:
            source = str(Path(work) / "video")
            target = str(Path(work) / "thumbnail.jpg")
            try:
                source_blob.download_to_filename(
                    source, if_generation_match=source_blob.generation, timeout=120
                )
                position = extract_frame(source, target, seconds)
            except (NotFound, RuntimeError, subprocess.SubprocessError):
                if source_uri != candidates[-1]:
                    continue
                raise
            target_blob.metadata = {
                "source_uri": source_uri,
                "source_generation": str(source_blob.generation),
                "frame_seconds": str(position),
            }
            target_blob.cache_control = "private, max-age=21600"
            try:
                target_blob.upload_from_filename(
                    target, content_type="image/jpeg", if_generation_match=0, timeout=30
                )
            except PreconditionFailed:
                # Another request completed this exact source generation and frame.
                pass
        return f"gs://{bucket_name}/{target_name}"
    raise FileNotFoundError("No source video is available for this review")
