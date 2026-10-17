import shutil
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from google.api_core.exceptions import NotFound

from app import thumbnails


def test_cleans_and_old_reviews_get_a_bounded_representative_frame():
    assert thumbnails.frame_seconds({}) == 1
    assert thumbnails.frame_seconds({"reportable": [{"start_seconds": float("nan")}]}) == 1
    assert thumbnails.frame_seconds({"reportable": [{"start_seconds": -2}]}) == 0
    assert (
        thumbnails.frame_seconds(
            {
                "reportable": [{"start_seconds": 60}],
                "coverage": {"duration_seconds": 10},
            }
        )
        == 9.9
    )
    assert (
        thumbnails.frame_seconds(
            {
                "case": {
                    "offences": [
                        {"atSeconds": 2, "included": False},
                        {"atSeconds": 5, "included": True},
                    ]
                }
            }
        )
        == 5
    )


def test_images_are_distinct_for_changed_video_generations_and_cited_moments():
    name = thumbnails.image_name("gs://b/uploads/a", 1, 2)
    assert name.startswith("thumbnails/") and name.endswith(".jpg")
    assert name != thumbnails.image_name("gs://b/uploads/a", 2, 2)
    assert name != thumbnails.image_name("gs://b/uploads/a", 1, 3)
    assert name == thumbnails.image_name("gs://b/uploads/a", "1", 2)


def test_existing_images_are_reused_without_downloading_or_reencoding(monkeypatch):
    monkeypatch.setenv("APP_PROJECT_ID", "test-project")
    source, target = Mock(generation=3), Mock()
    target.exists.return_value = True
    bucket = Mock()
    bucket.blob.side_effect = [source, target]
    client = Mock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(thumbnails.storage, "Client", lambda **kwargs: client)
    uri = thumbnails.render({"video_uri": "gs://bucket/uploads/a"})
    assert uri.startswith("gs://bucket/thumbnails/")
    source.download_to_filename.assert_not_called()
    target.upload_from_filename.assert_not_called()


@pytest.mark.parametrize("broken_annotation", [False, True])
def test_missing_or_broken_annotation_uses_the_original_and_uploads_a_jpeg_once(
    monkeypatch, broken_annotation
):
    monkeypatch.setenv("APP_PROJECT_ID", "test-project")
    missing, original, target = Mock(), Mock(generation=4), Mock()
    missing.reload.side_effect = None if broken_annotation else NotFound("missing")
    target.exists.return_value = False
    bucket = Mock()
    bucket.blob.side_effect = (
        [missing, target, original, target] if broken_annotation else [missing, original, target]
    )
    client = Mock()
    client.bucket.return_value = bucket
    monkeypatch.setattr(thumbnails.storage, "Client", lambda **kwargs: client)
    extract = (
        Mock(side_effect=[RuntimeError("invalid video"), 2])
        if broken_annotation
        else Mock(return_value=2)
    )
    monkeypatch.setattr(thumbnails, "extract_frame", extract)
    result = thumbnails.render(
        {
            "video_uri": "gs://bucket/uploads/a",
            "annotated_video_uri": "gs://bucket/annotated/a",
            "reportable": [{"start_seconds": 2}],
        }
    )
    assert result.startswith("gs://bucket/thumbnails/")
    assert target.upload_from_filename.call_args.kwargs["content_type"] == "image/jpeg"
    assert target.upload_from_filename.call_args.kwargs["if_generation_match"] == 0
    assert target.metadata["source_uri"] == "gs://bucket/uploads/a"
    original.download_to_filename.assert_called_once()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is required for rendering")
def test_real_ffmpeg_produces_a_small_jpeg_even_if_legacy_timestamp_is_past_the_end(tmp_path):
    source, target = str(tmp_path / "source.mp4"), str(tmp_path / "frame.jpg")
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=640x360:d=0.5",
            "-c:v",
            "libx264",
            "-y",
            source,
        ],
        check=True,
        timeout=30,
    )
    assert thumbnails.extract_frame(source, target, 99) == 0
    assert Path(target).read_bytes().startswith(b"\xff\xd8")
    size = (
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                target,
            ],
            timeout=10,
        )
        .decode()
        .strip()
    )
    assert size == "320,180"
