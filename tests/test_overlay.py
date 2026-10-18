import json
import shutil
import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

from app import overlay
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
    assert f"between(t,2.00,{2.0 + HOLD_SECONDS - BOUNDARY_GAP:.2f})" in second


def test_consecutive_boxes_never_overlap():
    import re

    filters = drawbox_filters([track([box(1.0), box(2.0), box(3.0)])], 100, 100)
    spans = [
        tuple(map(float, re.search(r"between\(t,([\d.]+),([\d.]+)\)", f).groups())) for f in filters
    ]
    assert all(spans[i][1] < spans[i + 1][0] for i in range(len(spans) - 1))


def test_the_last_box_is_held_rather_than_blinking_out():
    [only] = drawbox_filters([track([box(5.0)])], 100, 100)
    assert f"between(t,5.00,{5.0 + HOLD_SECONDS - BOUNDARY_GAP:.2f})" in only


def test_a_box_does_not_remain_over_an_absent_vehicle_until_the_next_detection():
    first, _ = drawbox_filters([track([box(1.0), box(5.0)])], 100, 100, hold_seconds=0.5)
    assert "between(t,1.00,1.49)" in first


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


def test_the_excerpt_covers_the_cited_seconds_with_room_either_side():
    from app.overlay import CLIP_PADDING, cited_span

    start, end = cited_span([track([box(10.0), box(14.0)])])
    assert start == 10.0 - CLIP_PADDING
    assert end > 14.0  # the last box is held, then padded


def test_an_excerpt_never_starts_before_the_video_does():
    from app.overlay import cited_span

    # A box at 1s with 3s of padding would otherwise ask ffmpeg to seek to -2s.
    assert cited_span([track([box(1.0)])])[0] == 0.0


def test_there_is_no_excerpt_without_boxes():
    from app.overlay import cited_span

    assert cited_span([]) is None
    assert cited_span([track([])]) is None


def test_boxes_move_with_the_cut():
    from app.overlay import shifted

    # A box at 12s in the original is at 2s in a clip that starts at 10s.
    [moved] = shifted([track([box(12.0), box(13.0)])], 10.0)
    assert [f["atSeconds"] for f in moved["frames"]] == [2.0, 3.0]
    # Everything else about the track is untouched.
    assert moved["vehicleTrackId"] == "v1"


def test_an_excerpt_and_a_full_render_do_not_share_a_name():
    from app.overlay import annotated_name

    assert annotated_name("uploads/a", "excerpt") != annotated_name("uploads/a", "annotated")


@pytest.mark.anyio
@pytest.mark.parametrize(
    "rotation,size",
    [
        (0, (1280, 720)),
        (90, (720, 1280)),
        (-90, (720, 1280)),
        (270, (720, 1280)),
        (180, (1280, 720)),
    ],
)
async def test_box_dimensions_follow_the_display_rotation(monkeypatch, rotation, size):
    process = Mock(
        communicate=AsyncMock(
            return_value=(
                json.dumps(
                    {
                        "streams": [
                            {
                                "width": 1280,
                                "height": 720,
                                "side_data_list": [{"rotation": rotation}],
                            }
                        ]
                    }
                ).encode(),
                b"",
            )
        )
    )
    monkeypatch.setattr(overlay.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await overlay.dimensions("phone.mp4") == size


@pytest.mark.anyio
async def test_legacy_rotate_tags_are_applied_when_there_is_no_display_matrix(monkeypatch):
    process = Mock(
        communicate=AsyncMock(
            return_value=(
                json.dumps(
                    {
                        "streams": [
                            {
                                "width": 1280,
                                "height": 720,
                                "tags": {"rotate": "-90"},
                            }
                        ]
                    }
                ).encode(),
                b"",
            )
        )
    )
    monkeypatch.setattr(overlay.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    assert await overlay.dimensions("phone.mp4") == (720, 1280)


@pytest.mark.anyio
@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe are required for the real-video regression",
)
@pytest.mark.parametrize("span", [None, (0.0, 0.5)])
async def test_phone_rotation_draws_boxes_on_the_displayed_pixels_for_video_and_excerpt(
    monkeypatch, tmp_path, span
):
    landscape, rotated, rendered = (
        tmp_path / name for name in ("coded.mp4", "phone.mp4", "out.mp4")
    )
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=320x180:r=10",
            "-t",
            "1",
            "-c:v",
            "libx264",
            str(landscape),
        ],
        check=True,
    )
    help_text = subprocess.check_output(
        ["ffmpeg", "-hide_banner", "-h", "full"], stderr=subprocess.DEVNULL
    ).decode()
    rotation_input = ["-display_rotation:v:0", "-90"] if "-display_rotation" in help_text else []
    rotation_output = [] if rotation_input else ["-metadata:s:v:0", "rotate=-90"]
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            *rotation_input,
            "-i",
            str(landscape),
            "-c",
            "copy",
            *rotation_output,
            str(rotated),
        ],
        check=True,
    )

    class Blob:
        def download_to_filename(self, destination):
            shutil.copyfile(rotated, destination)

        def upload_from_filename(self, source, content_type):
            shutil.copyfile(source, rendered)

    storage = Mock()
    storage.bucket.return_value.blob.return_value = Blob()
    monkeypatch.setattr(overlay.storage, "Client", lambda **kwargs: storage)
    monkeypatch.setenv("APP_PROJECT_ID", "test-project")
    assert await overlay.dimensions(str(rotated)) == (180, 320)
    uri = await overlay.render(
        "gs://test-videos/uploads/phone",
        [
            track(
                [
                    box(0, x=0.25, y=0.4, w=0.25, h=0.2),
                ]
            )
        ],
        span,
    )
    assert uri == f"gs://test-videos/{'excerpt' if span else 'annotated'}/phone"
    assert await overlay.dimensions(str(rendered)) == (180, 320)
    pixels = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(rendered),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    red = [
        (i // 3 % 180, i // 3 // 180)
        for i in range(0, len(pixels), 3)
        if pixels[i] > 150 and pixels[i + 1] < 80 and pixels[i + 2] < 80
    ]
    assert red
    x0, x1 = min(p[0] for p in red), max(p[0] for p in red)
    y0, y1 = min(p[1] for p in red), max(p[1] for p in red)
    assert 43 <= x0 <= 47 and 87 <= x1 <= 91
    assert 126 <= y0 <= 130 and 189 <= y1 <= 193
