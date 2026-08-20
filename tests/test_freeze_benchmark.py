"""Exercises the pure logic in tools/freeze_benchmark.py — no GUI, no cv2."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from freeze_benchmark import (  # noqa: E402
    build_benchmark_split,
    load_frame_records,
    sha256_of_file,
)


def _make_replay(tmp_path: Path, num_frames: int) -> Path:
    replay_dir = tmp_path / "replay"
    frames_dir = replay_dir / "frames"
    frames_dir.mkdir(parents=True)
    with (replay_dir / "frames.jsonl").open("w", encoding="utf-8") as f:
        for i in range(num_frames):
            file_name = f"frames/{i:06d}.png"
            (replay_dir / file_name).write_bytes(f"fake-png-bytes-{i}".encode())
            f.write(json.dumps({"frame_index": i, "file": file_name}) + "\n")
    return replay_dir


def test_sha256_of_file_matches_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "f.bin"
    path.write_bytes(b"hello")
    assert sha256_of_file(path) == hashlib.sha256(b"hello").hexdigest()


def test_load_frame_records_maps_position_to_record(tmp_path):
    replay_dir = _make_replay(tmp_path, num_frames=3)
    records = load_frame_records(replay_dir)
    assert set(records.keys()) == {0, 1, 2}
    assert records[1]["file"] == "frames/000001.png"


def test_build_benchmark_split_includes_hash_viewport_and_annotations(tmp_path):
    replay_dir = _make_replay(tmp_path, num_frames=5)
    annotations = {
        "format_version": 1,
        "frames": {
            "0": [{"cx": 1.0, "cy": 2.0, "half_size": 3.0}],
            "4": [],
        },
    }
    viewport = {"left": 0, "top": 0, "width": 1920, "height": 1080}

    benchmark = build_benchmark_split(
        replay_dir=replay_dir,
        replay_name="squares_test",
        viewport=viewport,
        annotations=annotations,
        frame_indices=[0, 4],
    )

    assert benchmark["replay"] == "squares_test"
    assert benchmark["viewport"] == viewport
    assert set(benchmark["frames"].keys()) == {"0", "4"}
    assert benchmark["frames"]["0"]["annotations"] == annotations["frames"]["0"]
    assert benchmark["frames"]["4"]["annotations"] == []
    assert benchmark["frames"]["0"]["file"] == "frames/000000.png"
    assert benchmark["frames"]["0"]["sha256"] == sha256_of_file(replay_dir / "frames/000000.png")


def test_build_benchmark_split_raises_if_frame_missing_annotation(tmp_path):
    replay_dir = _make_replay(tmp_path, num_frames=5)
    annotations = {"format_version": 1, "frames": {"0": []}}

    with pytest.raises(SystemExit):
        build_benchmark_split(
            replay_dir=replay_dir,
            replay_name="squares_test",
            viewport={},
            annotations=annotations,
            frame_indices=[0, 4],
        )
