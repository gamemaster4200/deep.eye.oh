# deep.eye.oh — architectural baseline

## What this project is

An autonomous agent for diep.io that interacts with the real game through
the same channels a human uses:

```
screen
    ↓
perception / state estimation
    ↓
internal game state
    ↓
decision-making policy
    ↓
ordinary keyboard + mouse input
```

We do not use the DOM, process memory, internal game state, the network
protocol, or other hidden data.

## Pipeline

```
Environment
    ↓
Observation
    ↓
StateEstimator
    ↓
GameState
    ↓
Policy
    ↓
Action
    ↓
Environment
```

## Constraints

- `Policy` must not depend on pixels, OpenCV, Windows input APIs, or any
  concrete `Environment`. It operates only on `GameState` and produces
  `Action`.
- `Observation` and `GameState` are different concepts: `Observation` is raw
  sensor data (e.g. a captured frame); `GameState` is the estimated internal
  representation derived from it via `StateEstimator`.
- Integration with the real game happens only through captured screen images
  and ordinary user input (keyboard/mouse). No DOM, process memory, network
  protocol, or other hidden data, ever.
- A future `simulator` will produce/consume the same `GameState`/`Action`
  representation as the real environment, so policies can be trained and
  tested off the real game and transferred back.
- Development proceeds as a sequence of small, measurable vertical slices.
  Do not build out the full vision stack, simulator, or policy subsystem
  ahead of actual need.

## Current status

Completed slices: bootstrap; `capture + replay v0` (screen capture,
Observation contract, replay recording/reading); control/focus-safety/
emergency-stop/debug-tooling v0 (Windows keyboard/mouse control gated by
an armed-window focus check, mouse cursor-target check, and an
independent emergency stop, plus replay/capture inspection CLI tooling).
No game-specific perception or policy logic exists yet.

`shape-perception-v0` (branch `feat/shape-perception-v0`, WIP): human
ground-truth annotation tooling (`tools/annotate_squares.py`,
`tools/freeze_benchmark.py`, `tools/evaluate_squares.py`) and a frozen
`squares_v0` benchmark (`benchmarks/squares_v0/`) for an upcoming
`SquareDetector`. See `benchmarks/squares_v0/README.md` for the annotation
and evaluation methodology. No detector/GameState/Policy/Action
implementation exists yet.
