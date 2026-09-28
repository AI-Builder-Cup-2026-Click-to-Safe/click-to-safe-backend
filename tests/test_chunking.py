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
