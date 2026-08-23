"""browser-informed-farming-v0: ties BrowserBridgeServer, browser
GameState parsing, coordinate calibration, BrowserPolicy, and the
existing safety-gated Controller into one working autonomous farming
loop.

Fail-closed by construction: no bridge connection yet, stale telemetry,
missing canvas info, a degenerate coordinate transform, or Controller
itself refusing input (ControlNotSafeError) all release every held input
and either skip the tick or stop the loop outright -- none of them keep
sending the last known-good action.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from deep_eye_oh import window_focus
from deep_eye_oh.browser_bridge import DEFAULT_PORT, BrowserBridgeServer
from deep_eye_oh.browser_game_state import BrowserGameState, ScreenTransform, compute_screen_transform
from deep_eye_oh.browser_policy import BrowserAction, BrowserPolicy, select_target
from deep_eye_oh.control import Controller, ControlNotSafeError

logger = logging.getLogger(__name__)

# A few multiples of the Oracle's own 250ms cache window (see
# deep.eye.oh.ext/extension/src/oracle.js CACHE_WINDOW_MS) -- long enough
# to tolerate one missed bridge poll, short enough that a genuinely dead
# bridge/tab is noticed quickly.
STALE_AFTER_S = 0.5
TICK_INTERVAL_S = 0.05  # ~20Hz decision loop
STATUS_PRINT_EVERY_N_TICKS = 10


@dataclass(frozen=True)
class _HeldInputs:
    move_keys: frozenset = frozenset()
    shooting: bool = False


def _canvas_origin(state: BrowserGameState) -> tuple[float, float] | None:
    """Self-position proxy: diep.io's camera follows the player, so the
    player's tank is always rendered at the canvas center. Empirically
    the simplest viable choice for v0 -- see CLAUDE.md/PR description for
    why this was not turned into a self-detection research project."""
    if state.canvas is None:
        return None
    return (state.canvas.width / 2.0, state.canvas.height / 2.0)


def _release_all(controller: Controller) -> _HeldInputs:
    controller.release_all()
    return _HeldInputs()


def _apply_action(
    controller: Controller, action: BrowserAction, held: _HeldInputs, transform: ScreenTransform
) -> _HeldInputs:
    if not action.has_target:
        return _release_all(controller)

    screen_x, screen_y = transform.apply(action.aim_x, action.aim_y)
    controller.move_mouse(screen_x, screen_y)

    if action.shoot and not held.shooting:
        controller.press_button("left")
    elif not action.shoot and held.shooting:
        controller.release_button("left")

    for key in held.move_keys - action.move_keys:
        controller.release_key(key)
    for key in action.move_keys - held.move_keys:
        controller.press_key(key)

    return _HeldInputs(move_keys=action.move_keys, shooting=action.shoot)


def _format_status(
    state: BrowserGameState | None,
    age: float | None,
    origin: tuple[float, float] | None,
    controller: Controller,
) -> str:
    if state is None or age is None:
        return "telemetry: none received yet | controller: " + ("armed" if controller.armed else "disarmed")
    if age > STALE_AFTER_S:
        return f"telemetry: STALE (age={age:.2f}s) | controller: " + ("armed" if controller.armed else "disarmed")

    counts: dict[str, int] = {}
    for shape in state.shapes:
        counts[shape.shape_class] = counts.get(shape.shape_class, 0) + 1
    counts_str = " ".join(f"{cls}={counts.get(cls, 0)}" for cls in ("square", "triangle", "pentagon"))

    target_str = "none"
    if origin is not None:
        nearest = select_target(state, origin)
        if nearest is not None:
            distance = math.hypot(nearest.cx - origin[0], nearest.cy - origin[1])
            target_str = f"{nearest.shape_class} @ ({nearest.cx:.0f},{nearest.cy:.0f}) dist={distance:.0f}"

    return (
        f"telemetry: alive age={age:.2f}s | shapes: {counts_str} | "
        f"target: {target_str} | controller: " + ("armed" if controller.armed else "disarmed")
    )


def run_farming_loop(
    *,
    port: int = DEFAULT_PORT,
    panic_key: str = "pause",
    stale_after_s: float = STALE_AFTER_S,
    tick_interval_s: float = TICK_INTERVAL_S,
    max_ticks: int | None = None,
) -> None:
    """Arms Controller on the current foreground window (the operator must
    have the diep.io browser tab focused before calling this), then
    repeatedly: reads the latest browser telemetry, fails closed if it is
    missing/stale/uncalibratable, otherwise picks a target via
    BrowserPolicy and applies it through Controller. Runs until max_ticks
    (None = forever / until Ctrl+C / until Controller trips)."""
    target = window_focus.arm_foreground_window()
    controller = Controller(panic_key=panic_key)
    bridge = BrowserBridgeServer(port=port)
    policy = BrowserPolicy()

    bridge.start()
    held = _HeldInputs()
    try:
        controller.arm(target)
        print(f"Armed on {target.title_at_arm!r}. Waiting for browser telemetry on port {port}...")

        tick = 0
        while max_ticks is None or tick < max_ticks:
            tick += 1
            now = time.monotonic()
            state = bridge.latest()
            age = bridge.age_s(now)
            origin = _canvas_origin(state) if state is not None else None

            transform = None
            if state is not None and state.canvas is not None:
                client_rect = window_focus.client_rect_on_screen(target)
                if client_rect is not None:
                    transform = compute_screen_transform(state.canvas, client_rect)

            usable = state is not None and age is not None and age <= stale_after_s and origin is not None and transform is not None

            try:
                if usable:
                    action = policy.decide(state, origin)
                    held = _apply_action(controller, action, held, transform)
                else:
                    held = _release_all(controller)
            except ControlNotSafeError as exc:
                logger.warning("Controller refused input (%s); stopping farming loop.", exc)
                break

            if tick % STATUS_PRINT_EVERY_N_TICKS == 0:
                print(_format_status(state, age, origin, controller))

            time.sleep(tick_interval_s)
    finally:
        controller.release_all()
        bridge.stop()
        controller.disarm()


def run_calibration_check(
    *, port: int = DEFAULT_PORT, panic_key: str = "pause", duration_s: float = 30.0
) -> None:
    """Debug mode: arms Controller and, once per second, moves the mouse
    (never shoots, never presses WASD) to the transformed screen position
    of the nearest visible neutral shape, printing both the Oracle-space
    and screen-space coordinates -- the "simple calibration/debug mode"
    to visually confirm the coordinate transform before trusting it for
    live farming."""
    target = window_focus.arm_foreground_window()
    controller = Controller(panic_key=panic_key)
    bridge = BrowserBridgeServer(port=port)
    bridge.start()
    try:
        controller.arm(target)
        print(f"Armed on {target.title_at_arm!r}. Calibration check for {duration_s:.0f}s (panic key to stop).")
        deadline = time.monotonic() + duration_s
        while time.monotonic() < deadline:
            state = bridge.latest()
            age = bridge.age_s()
            if state is None or age is None or age > STALE_AFTER_S or state.canvas is None:
                print("no fresh browser telemetry / canvas info yet")
                time.sleep(1.0)
                continue

            origin = _canvas_origin(state)
            client_rect = window_focus.client_rect_on_screen(target)
            transform = compute_screen_transform(state.canvas, client_rect) if client_rect else None
            nearest = select_target(state, origin) if origin else None

            if transform is None or nearest is None:
                print(f"shapes={len(state.shapes)} nearest=none transform={'ok' if transform else 'unavailable'}")
                time.sleep(1.0)
                continue

            screen_x, screen_y = transform.apply(nearest.cx, nearest.cy)
            try:
                controller.move_mouse(screen_x, screen_y)
            except ControlNotSafeError as exc:
                print(f"Controller refused move: {exc}; stopping.")
                break
            print(
                f"nearest={nearest.shape_class} browser=({nearest.cx:.1f},{nearest.cy:.1f}) "
                f"-> screen=({screen_x},{screen_y})"
            )
            time.sleep(1.0)
    finally:
        controller.disarm()
        bridge.stop()
