# squares_v0 benchmark

Human ground-truth benchmark for an upcoming `SquareDetector` (detects
"neutral squares" from a captured game frame). No detector implementation
exists yet — this benchmark is frozen ahead of that work.

## Splits

- `holdout.json` — 15 frames from `replays/squares_holdout`, 40 GT square
  instances.
- `dev.json` — 15 frames from `replays/squares_dev`, 96 GT square instances.

Each file is produced by `tools/freeze_benchmark.py` from a human-annotated
`replays/<name>/annotations.json` (see `tools/annotate_squares.py`) and is
self-contained: replay name, viewport, and per frozen frame an index, the
source PNG's relative path, its SHA-256, and the GT annotation list. The
hash lets `tools/evaluate_squares.py --verify` confirm the local replay
frames used for a benchmark run are exactly the frames that were annotated,
without committing the full-resolution replay dataset (`replays/` stays
gitignored; these two small JSON files are the tracked, canonical GT).

Frame-index lists are frozen and must not change.

## Annotation methodology (human GT)

- Ground truth is axis-aligned: each square is stored as `{cx, cy,
  half_size}`, where `half_size = max(width, height) / 2` — a bounding-box
  diagnostic, not a true oriented side length (the in-game square can be
  rotated).
- **Only squares fully within the frame are GT.** Squares clipped by the
  frame boundary are excluded — not annotated at all.
- Partially occluded squares that are still fully in-frame **are** valid GT
  objects. Where an occluding object hides a corner, the corner is
  extrapolated (estimated), not skipped, so the recorded box isn't
  under-sized.

## Evaluation semantics (fixed before detector work; see `tools/evaluate_squares.py`)

- Because frame-edge-clipped squares are never GT, a detector must not be
  penalized merely for noticing one. **v0 rule:** any prediction whose
  inferred axis-aligned bbox touches or intersects the frame boundary is
  ignored by the evaluator before GT matching.
- This filter is fixed benchmark semantics, decided independent of any
  detector's behavior — **do not tune it against holdout results.**
- Remaining predictions are matched against GT using the center-distance
  rule agreed separately (not yet implemented in `evaluate_squares.py` —
  see that module's docstring).
