from app.overlay import BOUNDARY_GAP, HOLD_SECONDS, annotated_name, drawbox_filters


def track(frames, vehicle="v1"):
    return {"vehicleTrackId": vehicle, "frames": frames}


def box(at, x=0.5, y=0.25, w=0.25, h=0.5):
    return {"atSeconds": at, "x": x, "y": y, "width": w, "height": h}


def test_a_box_scales_to_the_clips_pixels():
    [f] = drawbox_filters([track([box(3.0)])], 1280, 720)
    # 0.5, 0.25, 0.25, 0.5 of a 1280x720 frame.
    assert "x=640:y=180:w=320:h=360" in f


def test_a_box_is_shown_until_just_before_the_next_sample():
    first, second = drawbox_filters([track([box(1.0), box(2.0)])], 100, 100)
    # Ending exactly on the next start would draw both boxes on that frame.
    assert f"between(t,1.00,{2.0 - BOUNDARY_GAP:.2f})" in first
    assert f"between(t,2.00,{2.0 + HOLD_SECONDS:.2f})" in second


def test_consecutive_boxes_never_overlap():
    import re

    filters = drawbox_filters([track([box(1.0), box(2.0), box(3.0)])], 100, 100)
    spans = [
        tuple(map(float, re.search(r"between\(t,([\d.]+),([\d.]+)\)", f).groups())) for f in filters
    ]
    assert all(spans[i][1] < spans[i + 1][0] for i in range(len(spans) - 1))


def test_the_last_box_is_held_rather_than_blinking_out():
    [only] = drawbox_filters([track([box(5.0)])], 100, 100)
    assert f"between(t,5.00,{5.0 + HOLD_SECONDS:.2f})" in only


def test_two_vehicles_get_different_colours():
    filters = drawbox_filters([track([box(0)], "a"), track([box(0)], "b")], 100, 100)
    colours = {f.split("color=")[1].split("@")[0] for f in filters}
    assert len(colours) == 2


def test_a_tiny_box_still_has_a_drawable_size():
    # Rounding a sliver to zero would make drawbox fail outright.
    [f] = drawbox_filters([track([box(0, w=0.0001, h=0.0001)])], 100, 100)
    assert ":w=1:" in f and ":h=1:" in f


def test_no_tracks_means_no_filters():
    assert drawbox_filters([], 1280, 720) == []
    assert drawbox_filters([track([])], 1280, 720) == []


def test_the_annotated_copy_never_overwrites_the_upload():
    name = annotated_name("uploads/abc-123")
    assert name.startswith("annotated/") and "uploads/" not in name
