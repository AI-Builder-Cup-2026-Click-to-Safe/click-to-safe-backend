from app.agents import Manoeuvre, ObservedVehicle, TimelineEntry, VideoReport
from app.chunking import OVERLAP_SECONDS, WINDOW_SECONDS, merge, windows


def test_a_short_clip_is_one_window():
    assert windows(27.7) == [(0.0, 27.7)]
    assert windows(60.0) == [(0.0, 60.0)]


def test_windows_cover_the_whole_clip_in_order():
    spans = windows(600.0)
    assert spans[0][0] == 0.0
    assert spans[-1][1] == 600.0
    assert spans == sorted(spans)
    # No gap: each window starts before the previous one ended.
    assert all(spans[i + 1][0] <= spans[i][1] for i in range(len(spans) - 1))


def test_consecutive_windows_overlap_so_a_boundary_event_survives():
    spans = windows(600.0)
    for earlier, later in zip(spans, spans[1:]):
        assert earlier[1] - later[0] == OVERLAP_SECONDS


def test_no_window_is_longer_than_the_unit_except_an_absorbed_tail():
    spans = windows(302.0)
    assert all(end - start <= WINDOW_SECONDS + OVERLAP_SECONDS for start, end in spans)


def test_a_trailing_sliver_is_absorbed_rather_than_analysed_alone():
    # 553s leaves a tail shorter than the overlap once the windows are laid out.
    spans = windows(553.0)
    assert spans[-1][1] - spans[-1][0] > OVERLAP_SECONDS
    assert spans[-1][1] == 553.0


def test_an_empty_clip_yields_no_windows():
    assert windows(0) == []


def report(start: float, event: str, vehicle: str = "white_sedan") -> VideoReport:
    return VideoReport(
        timeline=[
            TimelineEntry(
                start_seconds=start, end_seconds=start + 4, event=event, vehicle_ids=[vehicle]
            )
        ],
        vehicles=[
            ObservedVehicle(
                vehicle_id=vehicle,
                appearance="white sedan",
                is_ego=False,
                manoeuvres=[
                    Manoeuvre(
                        start_seconds=start, end_seconds=start + 4, description=event, clarity=0.9
                    )
                ],
            )
        ],
        summary=event,
    )


def test_merge_shifts_timestamps_onto_the_clip_timeline():
    # The second window's analyst reports 10s; at an offset of 55s that is 65s.
    merged = merge([(0.0, report(12, "overtakes")), (55.0, report(10, "brakes"))])
    assert [round(e.start_seconds) for e in merged.timeline] == [12, 65]


def test_merge_orders_the_timeline_even_when_windows_arrive_out_of_order():
    merged = merge([(110.0, report(5, "cuts in")), (0.0, report(3, "queues"))])
    assert [round(e.start_seconds) for e in merged.timeline] == [3, 115]


def test_an_event_seen_in_the_overlap_is_reported_once():
    # Both windows see the same overtake at an absolute 57s.
    merged = merge([(0.0, report(57, "overtakes")), (55.0, report(2, "overtakes"))])
    assert len(merged.timeline) == 1


def test_two_vehicles_in_the_overlap_stay_separate():
    merged = merge(
        [
            (0.0, report(57, "overtakes", "white_sedan")),
            (55.0, report(2, "brakes", "silver_sedan")),
        ]
    )
    assert len(merged.timeline) == 2


def test_one_vehicle_seen_in_two_windows_becomes_one_entry():
    merged = merge([(0.0, report(10, "queues")), (55.0, report(20, "overtakes"))])
    assert [v.vehicle_id for v in merged.vehicles] == ["white_sedan"]
    assert len(merged.vehicles[0].manoeuvres) == 2


def test_merging_nothing_gives_an_empty_report():
    merged = merge([])
    assert merged.timeline == [] and merged.vehicles == []


def test_a_blob_nobody_reloaded_has_no_measurable_duration():
    """A freshly constructed blob has size None; the probe must say so, not crash."""
    from unittest.mock import Mock

    from app.duration import probe_duration

    assert probe_duration(Mock(size=None)) is None


def test_the_timeout_grows_with_the_clip_but_stays_bounded():
    from app.main import MAX_REVIEW_TIMEOUT, REVIEW_TIMEOUT, review_timeout

    assert review_timeout(None) == REVIEW_TIMEOUT
    assert review_timeout(30) == REVIEW_TIMEOUT
    # A long clip adds a window pass, so it must get more than a short one.
    assert review_timeout(255) > REVIEW_TIMEOUT
    assert review_timeout(600) > review_timeout(255)
    assert review_timeout(7200) <= MAX_REVIEW_TIMEOUT


def test_the_clip_limit_is_thirty_minutes():
    from app.chunking import MAX_CLIP_DURATION

    assert MAX_CLIP_DURATION == 1800.0


def test_a_ten_minute_clip_still_fits_its_budget():
    """Sequential windows: the budget must cover every window plus the agent loop."""
    from app.chunking import MAX_CLIP_DURATION
    from app.long_video import MAX_CONCURRENT_WINDOWS
    from app.main import MAX_REVIEW_TIMEOUT, review_timeout

    budget = review_timeout(MAX_CLIP_DURATION)
    count = len(windows(MAX_CLIP_DURATION))
    waves = -(-count // MAX_CONCURRENT_WINDOWS)
    # 45s a window was measured; the budget must beat that with the loop on top.
    assert budget >= waves * 45 + 600
    assert budget <= MAX_REVIEW_TIMEOUT


def test_a_caller_can_choose_a_longer_window():
    """A longer window means far fewer requests, so a far quicker review."""
    assert len(windows(600, window=300)) < len(windows(600))
    assert len(windows(600)) == 11


def test_a_window_as_long_as_the_clip_is_one_unchunked_request():
    # The whole video goes to the model in a single call.
    assert windows(1800, window=1800) == [(0.0, 1800)]
    assert windows(600, window=1800) == [(0.0, 600)]


def test_a_window_below_the_minimum_is_raised_to_it():
    from app.chunking import MIN_WINDOW_SECONDS

    # Asking for ten-second windows would make a long clip hundreds of requests.
    assert windows(600, window=10) == windows(600, window=MIN_WINDOW_SECONDS)


def test_a_larger_window_still_covers_the_whole_clip():
    spans = windows(1800, window=300)
    assert spans[0][0] == 0.0 and spans[-1][1] == 1800
    assert all(spans[i + 1][0] <= spans[i][1] for i in range(len(spans) - 1))


def test_the_budget_shrinks_when_the_window_grows():
    from app.main import review_timeout

    # Fewer requests to make, so less time needed.
    assert review_timeout(1800, window=1800) < review_timeout(1800, window=60)


def test_a_clip_that_cannot_finish_in_time_is_refused_up_front():
    """Twenty minutes of waiting then a timeout is worse than an immediate 422."""
    from app.main import windows_affordable

    # Half an hour read a minute at a time is thirty-three sequential calls.
    assert not windows_affordable(1800, 60)
    # The same clip in larger windows fits.
    assert windows_affordable(1800, 300)
    assert windows_affordable(1800, 1800)


def test_a_short_clip_is_always_affordable():
    from app.main import windows_affordable

    assert windows_affordable(30, 60)
    assert windows_affordable(None, 60)
