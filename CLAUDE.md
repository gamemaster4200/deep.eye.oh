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

## browser-informed-farming-v0 (this branch): a deliberate, isolated exception

This branch (`feat/browser-informed-farming-v0`) is a separate,
non-replacing path explicitly authorized to read data the browser client
already has -- the `deep.eye.oh.ext` Browser Oracle's `snapshot()`
(Canvas2D-observed neutral shape geometry), forwarded over a local
WebSocket bridge into a `BrowserGameState`. This is a narrow, explicit
exception to "no DOM/hidden data" above, not a repeal of it: it does not
touch process memory, the network *protocol*, or any state the Oracle
itself doesn't already read from the page's own Canvas2D render calls.
The canonical screen-only pipeline (`Observation -> StateEstimator ->
GameState -> Policy`) is untouched by this branch. All autonomous input
still goes exclusively through the existing `Controller` (`control.py`)
and its focus/emergency-stop safety gates -- this branch changes only
what feeds the policy, never how input is sent.
