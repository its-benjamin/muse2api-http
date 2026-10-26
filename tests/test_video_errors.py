from __future__ import annotations

import time

from muse2api.server import (
    classify_video_error,
    load_video_block,
    save_video_block,
    video_failure,
)


def test_region_restriction():
    text = (
        "Video generation isn't available for your account in your country or region "
        "— that's a lasting restriction, so retrying won't help."
    )
    assert classify_video_error(text) == "video_unavailable_region"


def test_quota_and_generic():
    assert classify_video_error("Your credits are exhausted") == "video_quota_exhausted"
    assert classify_video_error("the model returned no media") == "video_generation_failed"


def test_failure_keeps_model_prose_out_of_error():
    prose = "I can make you a still image instead."
    payload = video_failure("video_unavailable_region", prose)
    assert payload["code"] == "video_unavailable_region"
    assert "permanent" in payload["error"]
    assert "still image" not in payload["error"]
    assert payload["upstream_message"] == prose


def test_failure_omits_empty_upstream_message():
    payload = video_failure("video_generation_failed", "  ")
    assert set(payload) == {"error", "code"}


def test_video_block_round_trips_and_expires(tmp_path):
    path = tmp_path / "video_block.json"
    until = time.time() + 3600
    save_video_block(path, until)
    assert load_video_block(path) == until

    save_video_block(path, time.time() - 1)
    assert load_video_block(path) == 0.0


def test_corrupt_video_block_does_not_block(tmp_path):
    path = tmp_path / "video_block.json"
    path.write_text("not json", encoding="utf-8")
    assert load_video_block(path) == 0.0
    assert load_video_block(tmp_path / "missing.json") == 0.0
