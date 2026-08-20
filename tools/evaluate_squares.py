"""Benchmark-scoring utilities for the squares_v0 detection benchmark.

Development tooling only — not part of the production pipeline. No
SquareDetector exists yet, so this module currently provides only the two
pieces of evaluation semantics settled and frozen ahead of detector work:

- verify_frame_hashes / --verify CLI: confirms the local replay frames used
  for a benchmark run are byte-for-byte the frames that were annotated,
  via SHA-256 against a frozen benchmarks/<name>/<split>.json (see
  tools/freeze_benchmark.py).
- filter_boundary_clipped: drops predictions whose inferred axis-aligned
  bbox touches/intersects the frame boundary, before GT matching. Human GT
  excludes frame-edge-clipped squares by construction (see
  benchmarks/squares_v0/README.md), so a detector must not be penalized for
  noticing one. This filter must not be tuned on holdout data — it's fixed
  benchmark semantics, not a detector hyperparameter.

GT matching/scoring itself (the previously-agreed center-distance rule) is
deliberately NOT implemented here: its exact parameters were not available
when this module was written. Freezing that logic without confirming the
parameters would risk locking in wrong benchmark semantics, so it's left
for a follow-up once confirmed.

    python tools/evaluate_squares.py --verify --benchmark benchmarks/squares_v0/holdout.json --in replays/squares_holdout
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256_of_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_frame_hashes(benchmark: dict, replay_dir: Path) -> list[str]:
    """Return the sorted (by int value) list of frame-index keys whose local
    file hash doesn't match the frozen benchmark. Empty list means every
    frame verified clean."""
    mismatched = [
        frame_index
        for frame_index, entry in benchmark["frames"].items()
        if sha256_of_file(replay_dir / entry["file"]) != entry["sha256"]
    ]
    return sorted(mismatched, key=int)


def filter_boundary_clipped(
    predictions: list[dict], frame_width: int, frame_height: int
) -> list[dict]:
    """Drop predictions (each a {"cx","cy","half_size"} dict) whose
    axis-aligned bbox touches or crosses the frame boundary. Partially
    occluded-but-in-frame predictions are unaffected — only boundary
    clipping is filtered, matching the human GT's inclusion rule."""
    kept = []
    for p in predictions:
        x0, y0 = p["cx"] - p["half_size"], p["cy"] - p["half_size"]
        x1, y1 = p["cx"] + p["half_size"], p["cy"] + p["half_size"]
        if x0 <= 0 or y0 <= 0 or x1 >= frame_width or y1 >= frame_height:
            continue
        kept.append(p)
    return kept


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", required=True, help="verify local replay frame hashes against a frozen benchmark")
    parser.add_argument("--benchmark", required=True, help="path to benchmarks/<name>/<split>.json")
    parser.add_argument("--in", dest="in_", required=True, help="replay directory to verify")
    args = parser.parse_args(argv)

    benchmark = json.loads(Path(args.benchmark).read_text(encoding="utf-8"))
    mismatched = verify_frame_hashes(benchmark, Path(args.in_))
    if mismatched:
        raise SystemExit(
            f"hash mismatch for frame(s) {mismatched} — local replay frames "
            f"do not match the frozen benchmark at {args.benchmark}"
        )
    print(f"verified {len(benchmark['frames'])} frame(s) against {args.benchmark}: all hashes match")


if __name__ == "__main__":
    main()
