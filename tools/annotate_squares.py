"""Minimal click-based ground-truth annotation tool for neutral squares.

Development tooling only — not part of the production pipeline. Lets a
human drag axis-aligned boxes over one replay frame (via cv2.selectROIs),
then converts each box to the annotations.json schema shared with
GameState.Target/RawDetection: cx/cy = box center, half_size =
max(width, height) / 2 — an axis-aligned-bbox-based diagnostic, not a true
oriented side length (see shape-perception-v0 plan, section B/D).

    python tools/annotate_squares.py --in replays/squares_dev --frame 0
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


def annotate_frame(replay_dir: Path, frame_index: int) -> None:
    import cv2  # imported lazily: only the interactive path needs HighGUI

    reader = ReplayReader(replay_dir)
    obs = None
    for i, candidate in enumerate(reader):
        if i == frame_index:
            obs = candidate
            break
    if obs is None:
        raise SystemExit(f"frame {frame_index} out of range (frame_count={reader.frame_count})")

    frame_bgr = cv2.cvtColor(obs.frame, cv2.COLOR_RGB2BGR)
    window = "annotate squares - drag a box per square, ENTER/SPACE to confirm each, ESC when done"
    rois = cv2.selectROIs(window, frame_bgr)
    cv2.destroyAllWindows()

    annotations_path = replay_dir / "annotations.json"
    data = load_annotations(annotations_path)
    add_frame_annotations(data, frame_index, [tuple(int(v) for v in roi) for roi in rois])
    save_annotations(annotations_path, data)
    print(f"saved {len(rois)} annotation(s) for frame {frame_index} to {annotations_path}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_", required=True, help="replay directory")
    parser.add_argument("--frame", type=int, required=True)
    args = parser.parse_args(argv)
    annotate_frame(Path(args.in_), args.frame)


if __name__ == "__main__":
    main()
