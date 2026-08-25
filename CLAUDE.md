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

## browser-overlay-control-v0: in-page control overlay

An in-page overlay in `deep_eye_oh_ext` (toggled by `~`) lets a human
view live bot telemetry and issue text commands while the bot keeps
playing -- it is the bot's ears/mouth/dashboard, not part of its
autonomous brain. **Core invariant: removing/disabling the overlay must
not change the bot's autonomous policy/behavior at all, only remove the
ability to issue user commands.**

Extends the existing one-way (browser -> bot) WebSocket bridge
(`browser_bridge.py`) with a narrow, explicit, reviewed exception -- two
new inbound message types (`overlay_command`, `overlay_focus`) and three
new outbound ones (`overlay_command_result`, `bot_status`,
`overlay_key_event`) on the same connection. `overlay_command.py`'s
`dispatch_command()` is the entire command surface: it recognizes only
`pause`/`resume` as real capability (built from `Controller.release_all()`
+ a loop-level flag in `browser_farming.py`'s `run_farming_loop` -- no new
Controller call, no new game policy) and reports everything else
`unsupported`/`rejected` -- this module must never grow ad hoc behavior to
make a demo command "work".

**Physical/synthetic keyboard disambiguation (`physical_keyboard_hook.py`):**
a live spike showed that giving the overlay's command input real DOM
focus in the browser causes this project's own Controller-driven
`SendInput` keyboard traffic to be consumed as literal text by that
focused input instead of reaching the game -- `SendInput`-injected and
physical hardware keyboard events are NOT distinguishable at the DOM/JS
layer (both are `isTrusted: true`). The only place that distinction
exists is a Windows low-level keyboard hook's `LLKHF_INJECTED` flag.
`PhysicalKeyboardCapture`, active only while the overlay's command input
actually has focus, suppresses and relays *physical* keystrokes to the
overlay (which renders its own text buffer from them, since it is not
natively focused in this mode) while passing every `SendInput`-injected
event through completely untouched -- the bot's own keyboard input keeps
reaching the game exactly as if this hook did not exist. Deliberately a
separate, narrowly-scoped module (parallel to `win32_input.py`), not
folded into `Controller`.

The extension's overlay content script (`deep_eye_oh_ext/extension/src/
overlay.js`) never natively focuses any DOM element for command entry --
it renders its own text buffer purely from `overlay_key_event` relays
while `PhysicalKeyboardCapture` is active, and toggles/closes via the
`~`/backtick physical key (`KeyboardEvent.code === 'Backquote'`, never
`event.key`, so the toggle works under any keyboard layout, e.g.
Cyrillic) through the SAME relay when focused, or a normal isolated-world
`keydown` listener when it is not. Ordinary typed text is relayed to the
bot as `overlay_command`; a leading `/` is a LOCAL overlay-only command
(never sent to the bot); a leading `!` is refused outright (never sent
anywhere, never executed) -- the overlay must never become a shell.

**Exclusion-region scope note:** the overlay is excluded from the
current Browser Oracle Canvas2D perception path only by construction --
it is a plain DOM element (isolated-world content script, Shadow DOM),
never a canvas draw call, so `oracle.js`'s hooked
`CanvasRenderingContext2D` methods never see it; no filtering code was
needed or added. This is *not* a general guarantee for a future
screen-pixel `StateEstimator`: the canonical `Observation ->
StateEstimator -> GameState -> Policy` pipeline still has no perception
implementation (see above), and if it is ever built and used with the
overlay on screen, it will need its own, separate masking/exclusion
mechanism designed then, against its own real consumer.
