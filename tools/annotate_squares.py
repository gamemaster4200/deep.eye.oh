"""Minimal click-based ground-truth annotation tool for neutral squares.

Development tooling only — not part of the production pipeline. Walks a
fixed, pre-supplied list of replay frames. For each frame, left-click each
visible corner of a square (2-4 points; for a partially occluded corner,
click its extrapolated position rather than skipping it, or the bounding
box will be too small); ENTER commits the axis-aligned bounding box of the
clicked points as one annotation and starts the next square; ESC discards
the in-progress square's clicks without committing. Closing the frame
window (titlebar X) finalizes the frame with whatever squares were already
committed via ENTER and advances to the next frame. Each box converts to
the annotations.json schema shared with GameState.Target/RawDetection:
cx/cy = box center, half_size = max(width, height) / 2 — an
axis-aligned-bbox-based diagnostic, not a true oriented side length (see
shape-perception-v0 plan, section B/D).

    python tools/annotate_squares.py --in replays/squares_dev --out replays/squares_dev/annotations.json --frames 0,4,9,14,19,24,29,34,39,44,49,54,59,64,69

Re-running the same command resumes: frames already recorded in --out are
skipped unless named in --redo.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from deep_eye_oh.replay import ReplayReader

FORMAT_VERSION = 1


def roi_to_annotation(x: float, y: float, w: float, h: float) -> dict:
    """Convert one axis-aligned ROI (x, y, w, h) to the annotation schema."""
    return {
        "cx": x + w / 2,
        "cy": y + h / 2,
        "half_size": max(w, h) / 2,
    }


def load_annotations(path: Path) -> dict:
    if not path.exists():
        return {"format_version": FORMAT_VERSION, "frames": {}}
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_annotations(path: Path, data: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def add_frame_annotations(data: dict, frame_index: int, rois: list[tuple]) -> dict:
    """Set (overwrite) frame_index's annotation list from a list of (x, y, w, h) ROIs."""
    data["frames"][str(frame_index)] = [roi_to_annotation(*roi) for roi in rois]
    return data


def parse_int_csv(s: str) -> list[int]:
    """Parse a comma-separated string of ints, e.g. '0,4,9' -> [0, 4, 9]."""
    return [int(tok) for tok in s.split(",") if tok.strip()]


def select_frames_to_annotate(
    frame_indices: list[int], data: dict, redo: set[int] | None = None
) -> list[int]:
    """Return the subset of frame_indices, in original order, that still need
    annotation: any not already recorded in data["frames"], plus any index
    explicitly listed in `redo` (forces re-annotation regardless)."""
    redo = redo or set()
    completed = {int(k) for k in data.get("frames", {})}
    return [f for f in frame_indices if f in redo or f not in completed]


def collect_frames(reader, wanted: list[int]) -> dict:
    """Single pass over `reader`, collecting the frame for every index in
    `wanted`. Raises SystemExit before any GUI code runs if the reader is
    exhausted with frames still missing."""
    wanted_set = set(wanted)
    frames: dict = {}
    for i, obs in enumerate(reader):
        if i in wanted_set:
            frames[i] = obs.frame
            if len(frames) == len(wanted_set):
                break
    missing = sorted(wanted_set - frames.keys())
    if missing:
        raise SystemExit(
            f"frame(s) {missing} out of range (frame_count={reader.frame_count})"
        )
    return frames


def _cv2_select_rois(window_title: str, frame_rgb) -> list[tuple]:
    """Click-the-corners ROI selection: left-click each visible corner of a
    square (2-4 points), ENTER commits the axis-aligned bounding box of the
    clicked points as one annotation and starts the next square, ESC
    discards the in-progress square's clicks without committing. Closing
    the window (titlebar X) finalizes the frame with whatever squares were
    already committed via ENTER."""
    import cv2  # imported lazily: only this GUI-touching function needs HighGUI

    frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
    state = {"points": [], "rois": []}

    def redraw():
        canvas = frame_bgr.copy()
        for x, y, w, h in state["rois"]:
            cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 255, 0), 1)
        for px, py in state["points"]:
            cv2.drawMarker(canvas, (px, py), (0, 255, 255), cv2.MARKER_CROSS, 8, 1)
        if len(state["points"]) >= 2:
            xs = [p[0] for p in state["points"]]
            ys = [p[1] for p in state["points"]]
            cv2.rectangle(canvas, (min(xs), min(ys)), (max(xs), max(ys)), (0, 255, 255), 1)
        cv2.imshow(window_title, canvas)

    def on_mouse(event, x, y, _flags, _userdata):
        if event == cv2.EVENT_LBUTTONDOWN:
            state["points"].append((x, y))
            redraw()

    cv2.namedWindow(window_title)
    cv2.setMouseCallback(window_title, on_mouse)
    redraw()
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10):  # ENTER: commit current square as its points' AABB
            if len(state["points"]) >= 2:
                xs = [p[0] for p in state["points"]]
                ys = [p[1] for p in state["points"]]
                x0, y0 = min(xs), min(ys)
                state["rois"].append((x0, y0, max(xs) - x0, max(ys) - y0))
                state["points"] = []
                redraw()
        elif key == 27:  # ESC: discard current square's in-progress clicks
            state["points"] = []
            redraw()
        elif cv2.getWindowProperty(window_title, cv2.WND_PROP_VISIBLE) < 1:
            break  # window closed: finalize frame with whatever was committed
    cv2.destroyAllWindows()
    return state["rois"]


def run_annotation_session(
    replay_dir: Path,
    out_path: Path,
    frame_indices: list[int],
    redo: set[int] | None = None,
    select_rois_fn=_cv2_select_rois,
    reader_factory=ReplayReader,
) -> dict:
    redo = redo or set()
    data = load_annotations(out_path)
    to_annotate = select_frames_to_annotate(frame_indices, data, redo)

    if to_annotate:
        reader = reader_factory(replay_dir)
        frames_by_index = collect_frames(reader, to_annotate)
        for frame_index in to_annotate:
            position = frame_indices.index(frame_index) + 1
            title = (
                f"{replay_dir.name} - frame {frame_index} "
                f"({position}/{len(frame_indices)}) - "
                "click corners, ENTER commits square, ESC cancels square, "
                "close window when frame done"
            )
            rois = select_rois_fn(title, frames_by_index[frame_index])
            add_frame_annotations(data, frame_index, list(rois))
            save_annotations(out_path, data)  # autosave after EVERY completed frame

    completed = sum(1 for f in frame_indices if str(f) in data["frames"])
    squares = sum(len(data["frames"].get(str(f), [])) for f in frame_indices)
    return {
        "selected": len(frame_indices),
        "completed": completed,
        "squares": squares,
        "out_path": out_path,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_", required=True, help="replay directory")
    parser.add_argument("--out", required=True, help="output annotations.json path")
    parser.add_argument(
        "--frames",
        required=True,
        help="comma-separated fixed frame-index list, e.g. 0,4,9,14",
    )
    parser.add_argument(
        "--redo",
        default="",
        help="comma-separated frame indices to force re-annotation even if already completed",
    )
    args = parser.parse_args(argv)

    summary = run_annotation_session(
        replay_dir=Path(args.in_),
        out_path=Path(args.out),
        frame_indices=parse_int_csv(args.frames),
        redo=set(parse_int_csv(args.redo)),
    )
    print(
        f"selected {summary['selected']} frame(s), "
        f"completed {summary['completed']} frame(s), "
        f"{summary['squares']} square instance(s) annotated, "
        f"output: {summary['out_path']}"
    )


if __name__ == "__main__":
    main()
