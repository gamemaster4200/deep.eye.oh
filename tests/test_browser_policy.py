"""Tests for browser_policy.py: target selection, Action production, and
lead (projectile-speed-and-lead-v0)."""

from deep_eye_oh.browser_game_state import BrowserGameState, BrowserShape, CanvasInfo
from deep_eye_oh.browser_policy import NOOP, BrowserPolicy, compute_lead, select_target
from deep_eye_oh.projectile_tracking import ProjectileSpeedEstimate
from deep_eye_oh.target_tracking import TargetObservation


def _shape(shape_class, cx, cy, radius=10.0):
    return BrowserShape(shape_class=shape_class, cx=cx, cy=cy, radius=radius, timestamp_ms=0.0)


def _state(*shapes, canvas=None, circles=()):
    return BrowserGameState(
        shapes=tuple(shapes), circles=tuple(circles), canvas=canvas,
        polled_at_ms=0.0, performance_now_ms=None, received_at=0.0,
    )


ORIGIN = (0.0, 0.0)

CANVAS = CanvasInfo(width=1600, height=900, rect_left=0, rect_top=0, rect_width=1600, rect_height=900, device_pixel_ratio=1)


def test_select_target_none_when_no_shapes():
    assert select_target(_state(), ORIGIN) is None


def test_select_target_picks_nearest_of_same_class():
    near = _shape("square", 10, 0)
    far = _shape("square", 100, 0)
    assert select_target(_state(far, near), ORIGIN) is near


def test_select_target_class_weight_can_prefer_farther_higher_value_shape():
    # A pentagon (weight 1.6) at distance 50 costs 80; a square (weight
    # 1.0) at distance 60 costs 60 -- the square wins despite being farther
    # in raw pixels than... no, nearer. Use distances where the weighting
    # actually flips the outcome relative to plain nearest-distance.
    near_square = _shape("square", 55, 0)  # cost = 55 * 1.0 = 55
    far_pentagon = _shape("pentagon", 50, 0)  # cost = 50 * 1.6 = 80
    assert select_target(_state(near_square, far_pentagon), ORIGIN) is near_square

    close_pentagon = _shape("pentagon", 30, 0)  # cost = 30 * 1.6 = 48
    farther_square = _shape("square", 55, 0)  # cost = 55 * 1.0 = 55
    assert select_target(_state(close_pentagon, farther_square), ORIGIN) is close_pentagon


# ---------------------------------------------------------------------------
# select_target: canvas-bounds filtering (regression for the live bug where
# select_target picked shapes reported with a center outside the visible
# canvas -- e.g. square @ (376,-195) -- producing a mouse destination
# outside the armed browser window and tripping Controller's cursor-over-
# target safety gate mid-shot).
# ---------------------------------------------------------------------------


def test_select_target_rejects_negative_cx():
    offscreen = _shape("square", -5, 100)
    assert select_target(_state(offscreen, canvas=CANVAS), ORIGIN) is None


def test_select_target_rejects_negative_cy():
    offscreen = _shape("square", 100, -195)
    assert select_target(_state(offscreen, canvas=CANVAS), ORIGIN) is None


def test_select_target_rejects_cx_beyond_canvas_width():
    offscreen = _shape("square", CANVAS.width + 1, 100)
    assert select_target(_state(offscreen, canvas=CANVAS), ORIGIN) is None


def test_select_target_rejects_cy_beyond_canvas_height():
    offscreen = _shape("square", 100, CANVAS.height + 1)
    assert select_target(_state(offscreen, canvas=CANVAS), ORIGIN) is None


def test_select_target_accepts_boundary_points():
    # 0 <= cx <= width / 0 <= cy <= height is inclusive at the edges.
    corner = _shape("square", 0, 0)
    assert select_target(_state(corner, canvas=CANVAS), ORIGIN) is corner
    opposite_corner = _shape("square", CANVAS.width, CANVAS.height)
    assert select_target(_state(opposite_corner, canvas=CANVAS), ORIGIN) is opposite_corner


def test_select_target_still_picks_nearest_in_bounds_target_ignoring_offscreen_ones():
    offscreen_but_closer = _shape("square", 1209, -132)  # from the live bug report
    onscreen_farther = _shape("square", 700, 400)
    state = _state(offscreen_but_closer, onscreen_farther, canvas=CANVAS)
    assert select_target(state, ORIGIN) is onscreen_farther


def test_select_target_none_when_every_shape_is_offscreen():
    a = _shape("square", 376, -195)  # from the live bug report
    b = _shape("square", 1209, -132)  # from the live bug report
    assert select_target(_state(a, b, canvas=CANVAS), ORIGIN) is None


def test_select_target_no_bounds_filtering_without_canvas_info():
    # Without canvas info, bounds cannot be judged; existing (canvas=None)
    # callers/tests must keep working unfiltered.
    far_out = _shape("square", -1000, -1000)
    assert select_target(_state(far_out, canvas=None), ORIGIN) is far_out


def test_decide_noop_when_only_offscreen_shapes_visible():
    policy = BrowserPolicy()
    offscreen = _shape("square", -5, 100)
    action = policy.decide(_state(offscreen, canvas=CANVAS), ORIGIN)
    assert action is NOOP


# ---------------------------------------------------------------------------
# BrowserPolicy.decide
# ---------------------------------------------------------------------------


def test_decide_noop_when_no_shapes():
    policy = BrowserPolicy()
    assert policy.decide(_state(), ORIGIN) is NOOP


def test_decide_aims_and_shoots_at_selected_target():
    policy = BrowserPolicy()
    target = _shape("triangle", 40, 30)
    action = policy.decide(_state(target), ORIGIN)
    assert action.has_target
    assert action.aim_x == 40
    assert action.aim_y == 30
    assert action.shoot is True


def test_decide_movement_keys_toward_target_quadrants():
    policy = BrowserPolicy(movement_deadzone_px=1.0)

    down_right = policy.decide(_state(_shape("square", 50, 50)), ORIGIN)
    assert down_right.move_keys == frozenset({"d", "s"})

    up_left = policy.decide(_state(_shape("square", -50, -50)), ORIGIN)
    assert up_left.move_keys == frozenset({"a", "w"})

    straight_right = policy.decide(_state(_shape("square", 50, 0)), ORIGIN)
    assert straight_right.move_keys == frozenset({"d"})


def test_decide_no_movement_keys_within_deadzone():
    policy = BrowserPolicy(movement_deadzone_px=10.0)
    action = policy.decide(_state(_shape("square", 2, -3)), ORIGIN)
    assert action.move_keys == frozenset()
    assert action.has_target  # still aims/shoots even if not moving


def test_noop_has_no_target():
    assert NOOP.has_target is False
    assert NOOP.shoot is False
    assert NOOP.move_keys == frozenset()


# ---------------------------------------------------------------------------
# compute_lead (projectile-speed-and-lead-v0)
# ---------------------------------------------------------------------------


def _target(cx=100.0, cy=0.0, vx=0.0, vy=0.0, timestamp_ms=1000.0, confidence=0.9, radius=15.0):
    return TargetObservation(cx=cx, cy=cy, vx=vx, vy=vy, radius=radius, timestamp_ms=timestamp_ms, confidence=confidence)


def _speed(speed_px_s=500.0, confidence=0.9, sample_count=10, measured_at=0.0, last_updated=0.0):
    return ProjectileSpeedEstimate(
        speed_px_s=speed_px_s, confidence=confidence, sample_count=sample_count,
        measured_at=measured_at, last_updated=last_updated,
    )


SHOOTER = (0.0, 0.0)


def test_compute_lead_unavailable_without_target():
    result = compute_lead(shooter=SHOOTER, target=None, now_ms=1000.0, speed_estimate=_speed())
    assert not result.available
    assert result.reason == "no_target"
    assert result.aim_x is None and result.aim_y is None


def test_compute_lead_unavailable_when_target_stale():
    target = _target(timestamp_ms=0.0)
    result = compute_lead(shooter=SHOOTER, target=target, now_ms=10_000.0, speed_estimate=_speed())
    assert not result.available
    assert result.reason == "target_stale"


def test_compute_lead_unavailable_when_target_low_confidence():
    target = _target(confidence=0.1)
    result = compute_lead(shooter=SHOOTER, target=target, now_ms=1000.0, speed_estimate=_speed())
    assert not result.available
    assert result.reason == "target_low_confidence"


def test_compute_lead_unavailable_when_speed_estimate_missing():
    result = compute_lead(shooter=SHOOTER, target=_target(), now_ms=1000.0, speed_estimate=None)
    assert not result.available
    assert result.reason == "speed_unavailable"


def test_compute_lead_unavailable_when_speed_estimate_unavailable():
    unavailable = ProjectileSpeedEstimate(speed_px_s=None, confidence=0.0, sample_count=0, measured_at=0.0, last_updated=None)
    result = compute_lead(shooter=SHOOTER, target=_target(), now_ms=1000.0, speed_estimate=unavailable)
    assert not result.available
    assert result.reason == "speed_unavailable"


def test_compute_lead_unavailable_when_speed_low_confidence():
    result = compute_lead(shooter=SHOOTER, target=_target(), now_ms=1000.0, speed_estimate=_speed(confidence=0.05))
    assert not result.available
    assert result.reason == "speed_low_confidence"


def test_compute_lead_unavailable_when_no_intercept_solution():
    # Target receding directly away faster than the projectile -- no valid
    # positive-time solution exists (see test_intercept.py's equivalent).
    target = _target(cx=10.0, cy=0.0, vx=100.0, vy=0.0)
    result = compute_lead(shooter=SHOOTER, target=target, now_ms=1000.0, speed_estimate=_speed(speed_px_s=50.0))
    assert not result.available
    assert result.reason == "no_intercept_solution"


def test_compute_lead_available_stationary_target():
    target = _target(cx=100.0, cy=0.0, vx=0.0, vy=0.0)
    result = compute_lead(shooter=SHOOTER, target=target, now_ms=1000.0, speed_estimate=_speed(speed_px_s=50.0))
    assert result.available
    assert result.reason == "ok"
    assert result.aim_x == 100.0
    assert result.aim_y == 0.0
    assert result.intercept_t == 2.0


def test_compute_lead_available_moving_target_aims_ahead():
    # Target moving laterally -- the intercept aim point must be AHEAD of
    # the target's current position in the direction of motion, not at its
    # current position.
    target = _target(cx=100.0, cy=0.0, vx=0.0, vy=50.0)
    result = compute_lead(shooter=SHOOTER, target=target, now_ms=1000.0, speed_estimate=_speed(speed_px_s=200.0))
    assert result.available
    assert result.aim_y > 0.0, "aim point must lead ahead of the target's current position in its direction of travel"
    assert result.intercept_t is not None and result.intercept_t > 0.0


def test_compute_lead_rejects_future_timestamped_target_as_stale():
    # now_ms before the target's own timestamp is not a valid non-negative
    # age -- treat it the same as staleness rather than a negative age.
    target = _target(timestamp_ms=5000.0)
    result = compute_lead(shooter=SHOOTER, target=target, now_ms=1000.0, speed_estimate=_speed())
    assert not result.available
    assert result.reason == "target_stale"
