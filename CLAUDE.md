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

## projectile-speed-and-lead-v0: own-projectile speed + target lead

Built on browser-informed-farming-v0's `BrowserGameState`, now also
carrying `circles` (generic filled-circle observations from
`deep.eye.oh.ext`'s `oracle.js circles()` -- see that repo's
`feat/generic-circle-observation-v0`). Renderer ownership of a circle is
never assumed: `projectile_tracking.OwnProjectileTracker` infers likely
own projectiles purely by correlating circle observations against things
this process already knows (self position, current commanded aim
direction, whether we are currently shooting), never from anything the
Oracle claims about ownership.

`projectile_tracking.ProjectileSpeedEstimator` maintains an ADAPTIVE
`ProjectileSpeedEstimate` (robust central estimate + regime-shift
detection) from those correlated samples. There is deliberately no
`DIEP_BULLET_SPEED` constant anywhere in this codebase and never should
be: Bullet Speed upgrades change effective projectile speed mid-session,
so the estimator must track the CURRENT observed speed, not a
class/build lookup.

`target_tracking.TargetTracker` is a minimal single-target motion
tracker (one-to-one nearest-neighbor association, real observed
timestamps) feeding `intercept.solve_intercept` (a pure geometry module
with no dependency on Oracle/Controller/this project at all) via
`browser_policy.compute_lead()`. Lead is a minimal aim-point OVERRIDE
used only when target freshness/confidence and speed-estimate confidence
both clear a bar and the solver finds a valid positive-time intercept;
otherwise farming's existing shape-targeting behavior is unchanged --
lead never substitutes a guessed or stale aim point.

Safety-relevant behavior (`Controller`, focus/emergency-stop gates,
Oracle canvas provenance) is unchanged by this slice.
