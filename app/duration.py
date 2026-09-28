"""Read a video's duration from its container header.

Chunking needs the length before any model call, and Cloud Storage does not
record it. An MP4's duration sits in the `mvhd` box inside `moov`, so a ranged
read of the object's head is enough — no ffmpeg in the image, and the whole
video is never downloaded.

Some encoders place `moov` at the END of the file (the box order is not fixed),
so a head read that finds nothing falls back to reading the tail.
"""

import logging
import struct

logger = logging.getLogger(__name__)

# Large enough to hold moov in a front-loaded file, small enough to stay cheap.
PROBE_BYTES = 2 * 1024 * 1024


def _mvhd_duration(data: bytes) -> float | None:
    """Find an mvhd box and return its duration in seconds.

    mvhd is: version(1) flags(3) then, for version 0, creation(4) modification(4)
    timescale(4) duration(4); version 1 widens the times to 8 bytes.
    """
    index = data.find(b"mvhd")
    if index < 0:
        return None
    body = data[index + 4 :]
    if len(body) < 20:
        return None
    version = body[0]
    try:
        if version == 1:
            timescale, duration = struct.unpack(">IQ", body[20:32])
        else:
            timescale, duration = struct.unpack(">II", body[12:20])
    except struct.error:
        return None
    if not timescale or not duration:
        return None
    seconds = duration / timescale
    # A plausible clip: anything else means the bytes were not really an mvhd.
    return seconds if 0 < seconds < 86_400 else None


def probe_duration(blob) -> float | None:
    """Duration in seconds, or None when the header does not yield one."""
    size = blob.size or 0
    if not isinstance(size, int) or size <= 0:
        # Usually a blob nobody reloaded: without a size there is no range to read.
        logger.warning("Video size is unknown; cannot measure its duration")
        return None
    try:
        head = blob.download_as_bytes(start=0, end=min(PROBE_BYTES, size) - 1, timeout=60)
    except Exception as exc:
        logger.error("Could not read the video header (%s)", type(exc).__name__)
        return None

    seconds = _mvhd_duration(head)
    if seconds is not None:
        return seconds
    if size <= PROBE_BYTES:
        return None
    try:
        tail = blob.download_as_bytes(start=max(0, size - PROBE_BYTES), end=size - 1, timeout=60)
    except Exception as exc:
        logger.error("Could not read the video trailer (%s)", type(exc).__name__)
        return None
    return _mvhd_duration(tail)
