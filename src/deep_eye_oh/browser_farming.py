"""browser-informed-farming-v0: ties BrowserBridgeServer, browser
GameState parsing, coordinate calibration, BrowserPolicy, and the
existing safety-gated Controller into one working autonomous farming
loop.

Fail-closed by construction: no bridge connection yet, stale telemetry,
missing canvas info, a degenerate coordinate transform, or Controller
itself refusing input (ControlNotSafeError) all release every held input
and either skip the tick or stop the loop outright -- none of them keep
sending the last known-good action.

projectile-speed-and-lead-v0 adds own-projectile speed estimation and
target lead ON TOP of the existing farming behavior (see BrowserPolicy's
module docstring): each tick, likely-own-projectile circles feed a
ProjectileSpeedEstimator, a separate moving-target candidate stream feeds
a TargetTracker, and browser_policy.compute_lead() combines the two into
an aim-point OVERRIDE used only when it is confidently available --
farming's own shape-targeting/movement is otherwise unchanged, and a
missing/low-confidence lead simply falls back to it (see
_decide_action_for_tick).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

from deep_eye_oh import window_focus
from deep_eye_oh.browser_bridge import DEFAULT_PORT, BrowserBridgeServer
from deep_eye_oh.browser_game_state import BrowserGameState, ScreenTransform, compute_screen_transform
from deep_eye_oh.browser_policy import BrowserAction, BrowserPolicy, LeadResult, compute_lead, select_target
from deep_eye_oh.control import Controller, ControlNotSafeError
from deep_eye_oh.projectile_tracking import (
    MIN_SAMPLES_FOR_ESTIMATE,
    MUZZLE_RADIUS_PX,
    OwnProjectileTracker,
    ProjectileSpeedEstimate,
    ProjectileSpeedEstimator,
)
from deep_eye_oh.target_tracking import TargetCandidate, TargetObservation, TargetTracker
from deep_eye_oh.window_focus import TargetWindow

logger = logging.getLogger(__name__)

# A few multiples of the Oracle's own 250ms cache window (see
# deep.eye.oh.ext/extension/src/oracle.js CACHE_WINDOW_MS) -- long enough
# to tolerate one missed bridge poll, short enough that a genuinely dead
# bridge/tab is noticed quickly.
STALE_AFTER_S = 0.5
TICK_INTERVAL_S = 0.05  # ~20Hz decision loop
STATUS_PRINT_EVERY_N_TICKS = 10
LEAD_DIAGNOSTICS_PRINT_EVERY_N_TICKS = 40  # ~2s at the default 20Hz tick rate -- aggregated, not per-render-call

# A moving-target candidate must be at least this far from the self-
# position proxy to be considered -- excludes the player's own tank body
# (if it renders as a circle) from target candidates. Reuses
# projectile_tracking's own "near self" radius rather than inventing a
# second unproven magic number; both describe the same muzzle-adjacent
# zone around self.
SELF_EXCLUSION_RADIUS_PX = MUZZLE_RADIUS_PX


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
    controller: Controller,
    action: BrowserAction,
    held: _HeldInputs,
    transform: ScreenTransform,
    target: TargetWindow,
) -> _HeldInputs:
    if not action.has_target:
        return _release_all(controller)

    screen_x, screen_y = transform.apply(action.aim_x, action.aim_y)

    # Belt-and-suspenders: BrowserPolicy.select_target() already filters
    # out shapes whose Oracle-space center is outside the canvas (see
    # browser_policy.py), but the transform is a separate calculation and
    # the window can move/resize between reading telemetry and sending
    # input. Never begin or continue aiming/shooting/moving toward a
    # screen point outside the armed window -- treat it exactly like an
    # unusable tick (release everything) rather than risk the cursor
    # drifting out from under a held button.
    if not window_focus.point_is_over_target(target, screen_x, screen_y):
        return _release_all(controller)

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


def _aim_direction(
    last_aim_point: tuple[float, float] | None, origin: tuple[float, float] | None
) -> tuple[float, float] | None:
    """Direction (not normalized -- OwnProjectileTracker only uses its
    direction) from self toward the point we last commanded the mouse to,
    used to correlate newly-seen circles with our own most recent shot.
    None whenever either endpoint is unknown, or the two coincide (no
    meaningful direction)."""
    if last_aim_point is None or origin is None:
        return None
    dx = last_aim_point[0] - origin[0]
    dy = last_aim_point[1] - origin[1]
    if dx == 0 and dy == 0:
        return None
    return (dx, dy)


def _target_candidates(
    state: BrowserGameState, origin: tuple[float, float], claimed_positions: frozenset
) -> list[TargetCandidate]:
    """Generic circle observations offered to TargetTracker this tick --
    excludes anything OwnProjectileTracker just claimed as a likely own
    projectile, and anything within SELF_EXCLUSION_RADIUS_PX of self (the
    player's own tank body, if it renders as a circle). A v0 heuristic
    (see SELF_EXCLUSION_RADIUS_PX); real renderer evidence from the live
    smoke may refine this."""
    candidates = []
    for circle in state.circles:
        if (circle.cx, circle.cy) in claimed_positions:
            continue
        if math.hypot(circle.cx - origin[0], circle.cy - origin[1]) <= SELF_EXCLUSION_RADIUS_PX:
            continue
        candidates.append(TargetCandidate(cx=circle.cx, cy=circle.cy, radius=circle.radius, timestamp_ms=circle.timestamp_ms))
    return candidates


def _format_lead_diagnostics(
    *,
    circles_seen: int,
    projectile_tracks: int,
    likely_own_projectiles: int,
    speed_estimate: ProjectileSpeedEstimate,
    now_monotonic: float,
    target: TargetObservation | None,
    now_ms: float | None,
    origin: tuple[float, float] | None,
    lead: LeadResult,
    commanded_aim: tuple[float, float] | None,
) -> str:
    """Phase 9's ONE high-information diagnostic block -- see
    projectile-speed-and-lead-v0's PR description for the exact fields
    this is meant to make answerable in a single live run. Aggregated (see
    LEAD_DIAGNOSTICS_PRINT_EVERY_N_TICKS), never per-render-call."""
    lines = [
        f"circles_seen: {circles_seen}",
        f"likely_own_projectiles: {likely_own_projectiles}",
        f"projectile_tracks: {projectile_tracks}",
        "",
    ]

    if speed_estimate.available:
        age_s = (
            now_monotonic - speed_estimate.last_updated
            if speed_estimate.last_updated is not None else float("inf")
        )
        lines.append(f"bullet_speed: {speed_estimate.speed_px_s:.1f} px/s")
        lines.append(f"bullet_speed_confidence: {speed_estimate.confidence:.2f}")
        lines.append(f"bullet_speed_samples: {speed_estimate.sample_count}")
        lines.append(f"bullet_speed_age: {age_s:.2f} s")
    else:
        reason = "insufficient_samples" if speed_estimate.sample_count < MIN_SAMPLES_FOR_ESTIMATE else "stale_or_inconsistent"
        lines.append(f"bullet_speed: unavailable ({reason})")

    lines.append("")

    if target is not None:
        lines.append(f"target_now: ({target.cx:.0f}, {target.cy:.0f})")
        lines.append(f"target_velocity: ({target.vx:.1f}, {target.vy:.1f}) px/s")
        lines.append(f"target_speed: {target.speed_px_s:.1f} px/s")
        lines.append(f"target_confidence: {target.confidence:.2f}")
        target_age = f"{(now_ms - target.timestamp_ms) / 1000.0:.2f} s" if now_ms is not None else "unknown"
        lines.append(f"target_age: {target_age}")
        if origin is not None:
            target_range = math.hypot(target.cx - origin[0], target.cy - origin[1])
            lines.append(f"target_range: {target_range:.0f} px")
    else:
        lines.append("target_now: unavailable (no_target)")

    lines.append("")

    if lead.available:
        lines.append(f"intercept_t: {lead.intercept_t:.3f} s")
        lines.append(f"predicted_intercept: ({lead.aim_x:.0f}, {lead.aim_y:.0f})")
    else:
        lines.append(f"intercept: unavailable ({lead.reason})")

    commanded_aim_str = f"({commanded_aim[0]:.0f}, {commanded_aim[1]:.0f})" if commanded_aim is not None else "none"
    lines.append(f"commanded_aim: {commanded_aim_str}")

    return "\n".join(lines)


def _controller_status(controller: Controller) -> str:
    if controller.armed:
        return "armed"
    reason = controller.trip_reason
    return f"disarmed reason={reason}" if reason is not None else "disarmed"


def _format_status(
    state: BrowserGameState | None,
    age: float | None,
    origin: tuple[float, float] | None,
    controller: Controller,
) -> str:
    if state is None or age is None:
        return f"telemetry: none received yet | controller: {_controller_status(controller)}"
    if age > STALE_AFTER_S:
        return f"telemetry: STALE (age={age:.2f}s) | controller: {_controller_status(controller)}"

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
        f"target: {target_str} | controller: {_controller_status(controller)}"
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
    BrowserPolicy (optionally lead-overridden -- see module docstring) and
    applies it through Controller. Runs until max_ticks (None = forever /
    until Ctrl+C / until Controller trips)."""
    target = window_focus.arm_foreground_window()
    controller = Controller(panic_key=panic_key)
    bridge = BrowserBridgeServer(port=port)
    policy = BrowserPolicy()
    own_projectile_tracker = OwnProjectileTracker()
    speed_estimator = ProjectileSpeedEstimator()
    target_tracker = TargetTracker()

    bridge.start()
    held = _HeldInputs()
    # last_aim_point/last_shoot_active feed OwnProjectileTracker's NEXT
    # tick (see _aim_direction below) -- last_shoot_active is taken from
    # `held` (what Controller actually still holds after _apply_action,
    # e.g. False if the window-boundary check rejected the point and
    # released everything), so a rare rejected-point tick still correctly
    # suppresses new-track seeding next tick even though last_aim_point
    # itself reflects the INTENDED point, not confirmed pixel-for-pixel
    # delivery.
    last_aim_point: tuple[float, float] | None = None
    last_shoot_active = False
    try:
        controller.arm(target)
        print(f"Armed on {target.title_at_arm!r}. Waiting for browser telemetry on port {port}...")

        tick = 0
        while max_ticks is None or tick < max_ticks:
            tick += 1

            # An async trip (FocusWatcher/EmergencyStop, on their own
            # background threads) can disarm Controller between ticks
            # without this loop ever calling a gated method again -- the
            # "no usable telemetry" branch below only ever calls the
            # gate-exempt release_all(), which never raises. Without this
            # explicit check the loop would otherwise keep running
            # (printing "disarmed" every Nth tick) instead of stopping.
            if not controller.armed:
                print(f"Controller is no longer armed ({_controller_status(controller)}); stopping farming loop.")
                break

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

            # Own-projectile correlation, speed estimation, and target
            # tracking -- all driven purely by THIS tick's circle
            # observations and what we know we commanded last tick. Only
            # advanced on usable ticks: circles from a stale/uncalibratable
            # snapshot are not trustworthy input for any of this either.
            target_observation: TargetObservation | None = None
            lead = LeadResult(available=False, reason="no_state")
            circles_seen = 0
            speed_estimate = speed_estimator.estimate(now=now)
            if usable:
                circles_seen = len(state.circles)
                aim_dir = _aim_direction(last_aim_point, origin)
                speed_samples = own_projectile_tracker.update(
                    state.circles, now_ms=state.performance_now_ms or 0.0,
                    self_position=origin, aim_direction=aim_dir, shoot_active=last_shoot_active,
                )
                for sample in speed_samples:
                    speed_estimator.add_sample(sample.speed_px_s, now=now)
                speed_estimate = speed_estimator.estimate(now=now)

                candidates = _target_candidates(state, origin, own_projectile_tracker.claimed_positions)
                target_observation = target_tracker.update(candidates, now_ms=state.performance_now_ms or 0.0)

                lead = compute_lead(
                    shooter=origin, target=target_observation,
                    now_ms=state.performance_now_ms or 0.0, speed_estimate=speed_estimate,
                )

            commanded_aim: tuple[float, float] | None = None
            try:
                if usable:
                    action = policy.decide(state, origin)
                    if lead.available:
                        # Lead is a minimal aim-point OVERRIDE only -- never
                        # substituted when unavailable (see compute_lead's
                        # docstring); farming's own shape target/movement
                        # decision is otherwise untouched.
                        action = BrowserAction(aim_x=lead.aim_x, aim_y=lead.aim_y, move_keys=action.move_keys, shoot=True)
                    if action.has_target:
                        commanded_aim = transform.apply(action.aim_x, action.aim_y)
                    held = _apply_action(controller, action, held, transform, target)
                else:
                    held = _release_all(controller)
            except ControlNotSafeError as exc:
                logger.warning("Controller refused input (%s); stopping farming loop.", exc)
                break

            last_aim_point = (action.aim_x, action.aim_y) if usable and action.has_target else None
            last_shoot_active = held.shooting

            if tick % STATUS_PRINT_EVERY_N_TICKS == 0:
                print(_format_status(state, age, origin, controller))
            if tick % LEAD_DIAGNOSTICS_PRINT_EVERY_N_TICKS == 0:
                print(_format_lead_diagnostics(
                    circles_seen=circles_seen,
                    projectile_tracks=own_projectile_tracker.active_track_count,
                    likely_own_projectiles=len(own_projectile_tracker.claimed_positions),
                    speed_estimate=speed_estimate,
                    now_monotonic=now,
                    target=target_observation,
                    now_ms=state.performance_now_ms if state is not None else None,
                    origin=origin,
                    lead=lead,
                    commanded_aim=commanded_aim,
                ))

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
            if not controller.armed:
                print(f"Controller is no longer armed ({_controller_status(controller)}); stopping calibration check.")
                break

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
