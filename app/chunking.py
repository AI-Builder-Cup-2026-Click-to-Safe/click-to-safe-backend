"""Split a long clip into analysable windows and merge what comes back.

Gemini reads a time range of a video through VideoMetadata, so a long clip is
covered by several requests against the SAME object rather than by cutting it
into files. Nothing is re-uploaded and the container needs no ffmpeg.

Two things make the windows usable as one analysis. Consecutive windows OVERLAP,
because a manoeuvre that straddles a boundary would otherwise reach each analyst
as a fragment that constitutes no violation. And each window's timestamps come
back relative to its own start, so they are shifted by the window's offset
before anything is merged; without that every timestamp past the first minute is
wrong.
"""

from app.agents import TimelineEntry, VideoReport

# A minute is the unit the caller asked for. The overlap only has to be long
# enough to hold a lane change or an overtake whole.
WINDOW_SECONDS = 60.0
OVERLAP_SECONDS = 5.0
# Below this, one request covers the clip and the chunking path is skipped.
MIN_CHUNKED_DURATION = 60.0
# Windows are read one at a time, so the clip's length sets how long a review
# takes. Ten minutes is what fits comfortably inside the request's own deadline.
MAX_CLIP_DURATION = 600.0


def windows(duration: float) -> list[tuple[float, float]]:
    """Cover `duration` with overlapping windows, in order.

    Each window after the first starts OVERLAP_SECONDS before the previous one
    ended. A trailing remainder shorter than the overlap is absorbed into the
    last window rather than becoming a sliver nothing can be judged from.
    """
    if duration <= 0:
        return []
    if duration <= MIN_CHUNKED_DURATION:
        return [(0.0, duration)]

    spans: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = min(start + WINDOW_SECONDS, duration)
        spans.append((start, end))
        if end >= duration:
            break
        start = end - OVERLAP_SECONDS
    # Absorb a final sliver: it carries no context of its own.
    if len(spans) > 1 and spans[-1][1] - spans[-1][0] <= OVERLAP_SECONDS:
        second_last_start = spans[-2][0]
        spans[-2:] = [(second_last_start, duration)]
    return spans


def _shift(report: VideoReport, offset: float) -> VideoReport:
    """Move a window's timestamps onto the clip's own timeline."""
    for entry in report.timeline:
        entry.start_seconds += offset
        entry.end_seconds += offset
    for vehicle in report.vehicles:
        for move in vehicle.manoeuvres:
            move.start_seconds += offset
            move.end_seconds += offset
    return report


def _same_event(a: TimelineEntry, b: TimelineEntry) -> bool:
    """Two entries describe one event when they overlap in time and in vehicles.

    An event inside the overlap is reported by both neighbouring windows. Times
    are compared loosely because each analyst estimates the boundary itself.
    """
    if not (a.start_seconds < b.end_seconds and b.start_seconds < a.end_seconds):
        return False
    if not a.vehicle_ids or not b.vehicle_ids:
        return True
    return bool(set(a.vehicle_ids) & set(b.vehicle_ids))


def merge(reports: list[tuple[float, VideoReport]]) -> VideoReport:
    """Combine per-window reports into one, in clip order.

    `reports` is (offset, report) pairs. Vehicles are keyed by vehicle_id, which
    the instruction asks each analyst to derive from appearance, so the same car
    seen in two windows usually lands under one entry; when it does not, the
    duplicate is kept rather than guessed away.
    """
    merged = VideoReport()
    summaries: list[str] = []

    for offset, report in sorted(reports, key=lambda pair: pair[0]):
        shifted = _shift(report, offset)
        for entry in shifted.timeline:
            # Drop only what a neighbouring window already reported.
            if not any(_same_event(entry, kept) for kept in merged.timeline):
                merged.timeline.append(entry)
        for vehicle in shifted.vehicles:
            existing = next(
                (v for v in merged.vehicles if v.vehicle_id == vehicle.vehicle_id), None
            )
            if existing is None:
                merged.vehicles.append(vehicle)
                continue
            for move in vehicle.manoeuvres:
                if not any(
                    abs(move.start_seconds - kept.start_seconds) < OVERLAP_SECONDS
                    and move.description == kept.description
                    for kept in existing.manoeuvres
                ):
                    existing.manoeuvres.append(move)
        if report.summary:
            summaries.append(f"[{offset:.0f}s+] {report.summary}")

    merged.timeline.sort(key=lambda e: e.start_seconds)
    for vehicle in merged.vehicles:
        vehicle.manoeuvres.sort(key=lambda m: m.start_seconds)
    merged.summary = " ".join(summaries)
    return merged
