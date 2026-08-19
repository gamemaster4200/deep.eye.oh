"""Exercises the pure annotation-format conversion/parsing logic in
tools/annotate_squares.py — no cv2 GUI involved, so this stays safe for the
standard test suite."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from annotate_squares import (  # noqa: E402
    add_frame_annotations,
    load_annotations,
    roi_to_annotation,
    save_annotations,
)


def test_roi_to_annotation_square_box():
    ann = roi_to_annotation(x=100, y=200, w=40, h=40)
    assert ann == {"cx": 120, "cy": 220, "half_size": 20}


def test_roi_to_annotation_non_square_box_uses_max_dimension():
    # Not a true oriented side length — half of max(w, h), per the agreed rule.
    ann = roi_to_annotation(x=0, y=0, w=30, h=50)
    assert ann["half_size"] == 25
    assert ann["cx"] == 15
    assert ann["cy"] == 25


def test_load_annotations_missing_file_returns_empty_skeleton(tmp_path):
    data = load_annotations(tmp_path / "annotations.json")
    assert data == {"format_version": 1, "frames": {}}


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "annotations.json"
    data = {"format_version": 1, "frames": {"0": [{"cx": 1.0, "cy": 2.0, "half_size": 3.0}]}}
    save_annotations(path, data)
    assert load_annotations(path) == data
    with path.open() as f:
        raw = json.load(f)
    assert raw == data


def test_add_frame_annotations_converts_and_sets_frame_entry():
    data = {"format_version": 1, "frames": {}}
    add_frame_annotations(data, 5, [(10, 10, 20, 20), (0, 0, 10, 30)])
    assert data["frames"]["5"] == [
        {"cx": 20, "cy": 20, "half_size": 10},
        {"cx": 5, "cy": 15, "half_size": 15},
    ]


def test_add_frame_annotations_overwrites_existing_frame_entry():
    data = {"format_version": 1, "frames": {"2": [{"cx": 1, "cy": 1, "half_size": 1}]}}
    add_frame_annotations(data, 2, [(0, 0, 4, 4)])
    assert data["frames"]["2"] == [{"cx": 2, "cy": 2, "half_size": 2}]


def test_add_frame_annotations_empty_rois_records_zero_squares():
    data = {"format_version": 1, "frames": {}}
    add_frame_annotations(data, 7, [])
    assert data["frames"]["7"] == []
