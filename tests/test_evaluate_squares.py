"""Exercises the pure logic in tools/evaluate_squares.py — no GUI, no cv2."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from evaluate_squares import (  # noqa: E402
    filter_boundary_clipped,
    sha256_of_file,
    verify_frame_hashes,
)


def test_verify_frame_hashes_all_match(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "a.png").write_bytes(b"frame-a")
    (replay_dir / "b.png").write_bytes(b"frame-b")
    benchmark = {
        "frames": {
            "0": {"file": "a.png", "sha256": sha256_of_file(replay_dir / "a.png")},
            "4": {"file": "b.png", "sha256": sha256_of_file(replay_dir / "b.png")},
        }
    }
    assert verify_frame_hashes(benchmark, replay_dir) == []


def test_verify_frame_hashes_detects_mismatch(tmp_path):
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    (replay_dir / "a.png").write_bytes(b"frame-a")
    (replay_dir / "b.png").write_bytes(b"frame-b")
    benchmark = {
        "frames": {
            "0": {"file": "a.png", "sha256": sha256_of_file(replay_dir / "a.png")},
            "4": {"file": "b.png", "sha256": "0" * 64},
        }
    }
    assert verify_frame_hashes(benchmark, replay_dir) == ["4"]


def test_filter_boundary_clipped_keeps_fully_interior_prediction():
    preds = [{"cx": 100, "cy": 100, "half_size": 10}]
    assert filter_boundary_clipped(preds, frame_width=1920, frame_height=1080) == preds


def test_filter_boundary_clipped_drops_left_edge_touching():
    preds = [{"cx": 5, "cy": 100, "half_size": 10}]  # x0 = -5
    assert filter_boundary_clipped(preds, frame_width=1920, frame_height=1080) == []


def test_filter_boundary_clipped_drops_top_edge_touching():
    preds = [{"cx": 100, "cy": 3, "half_size": 10}]  # y0 = -7
    assert filter_boundary_clipped(preds, frame_width=1920, frame_height=1080) == []


def test_filter_boundary_clipped_drops_right_edge_touching():
    preds = [{"cx": 1915, "cy": 100, "half_size": 10}]  # x1 = 1925 >= 1920
    assert filter_boundary_clipped(preds, frame_width=1920, frame_height=1080) == []


def test_filter_boundary_clipped_drops_bottom_edge_touching():
    preds = [{"cx": 100, "cy": 1075, "half_size": 10}]  # y1 = 1085 >= 1080
    assert filter_boundary_clipped(preds, frame_width=1920, frame_height=1080) == []


def test_filter_boundary_clipped_keeps_some_drops_others():
    preds = [
        {"cx": 100, "cy": 100, "half_size": 10},  # interior, kept
        {"cx": 5, "cy": 100, "half_size": 10},  # clipped, dropped
    ]
    result = filter_boundary_clipped(preds, frame_width=1920, frame_height=1080)
    assert result == [preds[0]]
