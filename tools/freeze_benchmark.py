"""One-shot tool: freeze a human-annotated fixed frame set into a tracked
benchmark definition under benchmarks/<name>/<split>.json.

Development tooling only — not part of the production pipeline. The local
replays/<name>/annotations.json produced by tools/annotate_squares.py stays
a working copy under a gitignored directory; this tool snapshots the
selected frames' ground truth plus a SHA-256 of each source frame's file
into one small, git-tracked JSON, so the frozen benchmark survives without
committing the full-resolution replay dataset. The hash lets tooling later
confirm the local replay frames used for a benchmark run are exactly the
frames that were annotated.

    python tools/freeze_benchmark.py --in replays/squares_holdout --annotations replays/squares_holdout/annotations.json --replay-name squares_holdout --frames 0,4,9,14,18,23,28,33,37,42,47,51,56,61,66 --out benchmarks/squares_v0/holdout.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_frame_records(replay_dir: Path) -> dict[int, dict]:
    """Map iteration position -> raw frames.jsonl record. Uses position
    (line index), matching the position-based frame-index convention used
    throughout this project's replay tooling (ReplayReader iteration order,
    cli.py's _cmd_replay_inspect, annotate_squares.py)."""
    records: dict[int, dict] = {}
    with (replay_dir / "frames.jsonl").open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            records[i] = json.loads(line)
    return records


def build_benchmark_split(
    replay_dir: Path,
    replay_name: str,
    viewport: dict,
    annotations: dict,
    frame_indices: list[int],
) -> dict:
    """Build the frozen benchmark-split dict for `frame_indices`. Every
    index must already have a human annotation entry (raises SystemExit
    otherwise) and a corresponding frames.jsonl record."""
    records = load_frame_records(replay_dir)
    frames: dict[str, dict] = {}
    for i in frame_indices:
        key = str(i)
        if key not in annotations.get("frames", {}):
            raise SystemExit(f"frame {i} has no human annotation in the given annotations.json")
        if i not in records:
            raise SystemExit(f"frame {i} has no frames.jsonl record in {replay_dir}")
        record = records[i]
        frames[key] = {
            "file": record["file"],
            "sha256": sha256_of_file(replay_dir / record["file"]),
            "annotations": annotations["frames"][key],
        }
    return {
        "format_version": 1,
        "replay": replay_name,
        "viewport": viewport,
        "frames": frames,
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="in_", required=True, help="replay directory")
    parser.add_argument("--annotations", required=True, help="path to the human annotations.json")
    parser.add_argument("--replay-name", required=True, help="logical replay name recorded in the benchmark file")
    parser.add_argument("--frames", required=True, help="comma-separated frozen frame-index list")
    parser.add_argument("--out", required=True, help="output benchmark split path, e.g. benchmarks/squares_v0/holdout.json")
    args = parser.parse_args(argv)

    replay_dir = Path(args.in_)
    manifest = json.loads((replay_dir / "manifest.json").read_text(encoding="utf-8"))
    annotations = json.loads(Path(args.annotations).read_text(encoding="utf-8"))
    frame_indices = [int(tok) for tok in args.frames.split(",") if tok.strip()]

    benchmark = build_benchmark_split(
        replay_dir=replay_dir,
        replay_name=args.replay_name,
        viewport=manifest["viewport"],
        annotations=annotations,
        frame_indices=frame_indices,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(benchmark, f, indent=2)
        f.write("\n")
    squares = sum(len(entry["annotations"]) for entry in benchmark["frames"].values())
    print(f"froze {len(frame_indices)} frame(s), {squares} square instance(s) -> {out_path}")


if __name__ == "__main__":
    main()
