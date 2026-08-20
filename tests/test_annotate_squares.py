"""Exercises the pure annotation-format conversion/parsing logic in
tools/annotate_squares.py — no cv2 GUI involved, so this stays safe for the
standard test suite."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from annotate_squares import (  # noqa: E402
    add_frame_annotations,
    collect_frames,
    load_annotations,
    parse_int_csv,
    roi_to_annotation,
    run_annotation_session,
    save_annotations,
    select_frames_to_annotate,
)


class _FakeObs:
    def __init__(self, frame):
        self.frame = frame


class _FakeReader:
    """Duck-types the subset of ReplayReader used by collect_frames: an
    iterable of objects with a .frame attribute, plus .frame_count."""

    def __init__(self, frame_count: int):
        self.frame_count = frame_count

    def __iter__(self):
        return iter(_FakeObs(f"frame-{i}") for i in range(self.frame_count))


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


def test_parse_int_csv_parses_comma_separated_ints():
    assert parse_int_csv("0,4,9") == [0, 4, 9]
    assert parse_int_csv("") == []


def test_select_frames_to_annotate_none_completed_returns_all_in_order():
    data = {"format_version": 1, "frames": {}}
    assert select_frames_to_annotate([0, 4, 9], data) == [0, 4, 9]


def test_select_frames_to_annotate_some_completed_are_skipped():
    data = {"format_version": 1, "frames": {"0": [], "9": []}}
    assert select_frames_to_annotate([0, 4, 9, 14], data) == [4, 14]


def test_select_frames_to_annotate_all_completed_returns_empty_list():
    data = {"format_version": 1, "frames": {"0": [], "4": []}}
    assert select_frames_to_annotate([0, 4], data) == []


def test_select_frames_to_annotate_redo_forces_reinclusion():
    data = {"format_version": 1, "frames": {"0": [], "4": []}}
    assert select_frames_to_annotate([0, 4], data, redo={4}) == [4]


def test_collect_frames_gathers_all_wanted_frames_in_one_pass():
    reader = _FakeReader(frame_count=20)
    frames = collect_frames(reader, [0, 9, 4])
    assert frames == {0: "frame-0", 9: "frame-9", 4: "frame-4"}


def test_collect_frames_missing_frame_raises_systemexit():
    reader = _FakeReader(frame_count=5)
    with pytest.raises(SystemExit) as exc_info:
        collect_frames(reader, [0, 4, 9])
    message = str(exc_info.value)
    assert "9" in message
    assert "frame_count=5" in message


def test_run_annotation_session_out_of_range_raises_before_any_gui_call(tmp_path):
    calls = []

    def failing_reader_factory(_replay_dir):
        return _FakeReader(frame_count=2)

    def counting_select_rois_fn(_title, _frame):
        calls.append(1)
        return []

    with pytest.raises(SystemExit):
        run_annotation_session(
            replay_dir=tmp_path,
            out_path=tmp_path / "annotations.json",
            frame_indices=[0, 5],
            reader_factory=failing_reader_factory,
            select_rois_fn=counting_select_rois_fn,
        )
    assert calls == []


def test_run_annotation_session_resumes_skips_already_completed_frames(tmp_path):
    out_path = tmp_path / "annotations.json"
    save_annotations(
        out_path,
        {"format_version": 1, "frames": {"0": [{"cx": 1, "cy": 1, "half_size": 1}]}},
    )
    calls = []

    def select_rois_fn(_title, _frame):
        calls.append(1)
        return [(0, 0, 10, 10)]

    run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0, 4],
        reader_factory=lambda _d: _FakeReader(frame_count=10),
        select_rois_fn=select_rois_fn,
    )
    assert calls == [1]
    data = load_annotations(out_path)
    assert data["frames"]["0"] == [{"cx": 1, "cy": 1, "half_size": 1}]
    assert data["frames"]["4"] == [{"cx": 5.0, "cy": 5.0, "half_size": 5.0}]


def test_run_annotation_session_redo_reannotates_specified_frame(tmp_path):
    out_path = tmp_path / "annotations.json"
    save_annotations(
        out_path,
        {"format_version": 1, "frames": {"0": [{"cx": 1, "cy": 1, "half_size": 1}]}},
    )

    run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0],
        redo={0},
        reader_factory=lambda _d: _FakeReader(frame_count=10),
        select_rois_fn=lambda _title, _frame: [(0, 0, 4, 4)],
    )
    data = load_annotations(out_path)
    assert data["frames"]["0"] == [{"cx": 2, "cy": 2, "half_size": 2}]


def test_run_annotation_session_autosaves_after_each_completed_frame(tmp_path):
    out_path = tmp_path / "annotations.json"
    seen_after_first = {}

    def select_rois_fn(title, _frame):
        if "frame 4" in title:
            seen_after_first.update(load_annotations(out_path)["frames"])
        return [(0, 0, 2, 2)]

    run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0, 4],
        reader_factory=lambda _d: _FakeReader(frame_count=10),
        select_rois_fn=select_rois_fn,
    )
    assert "0" in seen_after_first


def test_run_annotation_session_esc_with_zero_rois_records_empty_list(tmp_path):
    out_path = tmp_path / "annotations.json"
    run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0],
        reader_factory=lambda _d: _FakeReader(frame_count=10),
        select_rois_fn=lambda _title, _frame: [],
    )
    data = load_annotations(out_path)
    assert data["frames"]["0"] == []


def test_run_annotation_session_progress_position_reflects_full_selected_set(tmp_path):
    out_path = tmp_path / "annotations.json"
    save_annotations(out_path, {"format_version": 1, "frames": {"0": []}})
    titles = []

    def select_rois_fn(title, _frame):
        titles.append(title)
        return []

    run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0, 4, 9, 14],
        reader_factory=lambda _d: _FakeReader(frame_count=20),
        select_rois_fn=select_rois_fn,
    )
    assert any("frame 4 (2/4)" in t for t in titles)


def test_run_annotation_session_all_completed_skips_reader_and_gui_entirely(tmp_path):
    out_path = tmp_path / "annotations.json"
    save_annotations(
        out_path,
        {"format_version": 1, "frames": {"0": [{"cx": 1, "cy": 1, "half_size": 1}]}},
    )

    def failing_reader_factory(_d):
        raise AssertionError("reader_factory should not be called")

    def failing_select_rois_fn(_title, _frame):
        raise AssertionError("select_rois_fn should not be called")

    summary = run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0],
        reader_factory=failing_reader_factory,
        select_rois_fn=failing_select_rois_fn,
    )
    assert summary["selected"] == 1
    assert summary["completed"] == 1


def test_run_annotation_session_summary_counts(tmp_path):
    out_path = tmp_path / "annotations.json"
    save_annotations(
        out_path,
        {"format_version": 1, "frames": {"0": [{"cx": 1, "cy": 1, "half_size": 1}]}},
    )

    summary = run_annotation_session(
        replay_dir=tmp_path,
        out_path=out_path,
        frame_indices=[0, 4],
        reader_factory=lambda _d: _FakeReader(frame_count=10),
        select_rois_fn=lambda _title, _frame: [(0, 0, 2, 2), (10, 10, 4, 4)],
    )
    assert summary == {
        "selected": 2,
        "completed": 2,
        "squares": 3,
        "out_path": out_path,
    }
