from __future__ import annotations

import math

from deep_eye_oh.intercept import solve_intercept

ORIGIN = (0.0, 0.0)


def test_stationary_target():
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (0.0, 0.0), 50.0)
    assert solution is not None
    assert math.isclose(solution.t, 0.2, rel_tol=1e-6)
    assert math.isclose(solution.aim_x, 10.0, abs_tol=1e-6)
    assert math.isclose(solution.aim_y, 0.0, abs_tol=1e-6)


def test_lateral_target():
    solution = solve_intercept(ORIGIN, (0.0, 10.0), (5.0, 0.0), 50.0)
    assert solution is not None
    assert math.isclose(solution.t, 0.2010, rel_tol=1e-3)
    target_at_t = (5.0 * solution.t, 10.0)
    dist = math.hypot(target_at_t[0], target_at_t[1])
    assert math.isclose(dist, 50.0 * solution.t, rel_tol=1e-6)
    assert math.isclose(solution.aim_x, target_at_t[0], abs_tol=1e-6)
    assert math.isclose(solution.aim_y, target_at_t[1], abs_tol=1e-6)


def test_approaching_target():
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (-5.0, 0.0), 50.0)
    assert solution is not None
    assert math.isclose(solution.t, 0.1818, rel_tol=1e-3)
    target_at_t = (10.0 - 5.0 * solution.t, 0.0)
    dist = math.hypot(*target_at_t)
    assert math.isclose(dist, 50.0 * solution.t, rel_tol=1e-6)


def test_receding_reachable_target():
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (20.0, 0.0), 50.0)
    assert solution is not None
    assert math.isclose(solution.t, 1.0 / 3.0, rel_tol=1e-3)
    target_at_t = (10.0 + 20.0 * solution.t, 0.0)
    dist = math.hypot(*target_at_t)
    assert math.isclose(dist, 50.0 * solution.t, rel_tol=1e-6)


def test_receding_unreachable_target_returns_none():
    # Target moving directly away faster than the projectile -- can never
    # be caught, regardless of how much time passes.
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (100.0, 0.0), 50.0)
    assert solution is None


def test_tangent_near_zero_discriminant():
    # Constructed so discriminant == 0 exactly (a repeated root) at a
    # positive t -- see module docstring in this test's authoring notes:
    # shooter=(0,0), target=(3,4), velocity=(0,-10), u=6 -> t=0.625.
    solution = solve_intercept(ORIGIN, (3.0, 4.0), (0.0, -10.0), 6.0)
    assert solution is not None
    assert math.isclose(solution.t, 0.625, rel_tol=1e-6)
    assert math.isclose(solution.aim_x, 3.0, abs_tol=1e-6)
    assert math.isclose(solution.aim_y, 4.0 - 10.0 * 0.625, abs_tol=1e-6)


def test_linear_degeneracy_with_positive_solution():
    # |target velocity| == projectile_speed exactly -> the t^2 coefficient
    # vanishes (a == 0) and this becomes a linear equation.
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (-30.0, 40.0), 50.0)
    assert solution is not None
    assert math.isclose(solution.t, 1.0 / 6.0, rel_tol=1e-3)
    target_at_t = (10.0 - 30.0 * solution.t, 40.0 * solution.t)
    dist = math.hypot(*target_at_t)
    assert math.isclose(dist, 50.0 * solution.t, rel_tol=1e-6)


def test_linear_degeneracy_no_positive_solution_returns_none():
    # Moving directly away in a straight line at exactly the projectile's
    # own speed -- asymptotically parallel, never caught.
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (50.0, 0.0), 50.0)
    assert solution is None


def test_linear_degeneracy_zero_b_returns_none():
    # a == 0 and b == 0 (target moving exactly perpendicular at the
    # projectile's own speed): the equation degenerates to "100 == 0",
    # never satisfied for any t.
    solution = solve_intercept(ORIGIN, (10.0, 0.0), (0.0, 50.0), 50.0)
    assert solution is None


def test_target_already_at_shooter_with_zero_velocity_returns_none():
    # a == b == c == 0: every t "solves" |0|=u*t only at t=0, which is not
    # a positive solution -- there is no single well-defined minimum
    # positive t, so this must fail closed rather than guess t=0 or t=inf.
    solution = solve_intercept(ORIGIN, ORIGIN, (0.0, 0.0), 50.0)
    assert solution is None


def test_zero_or_negative_projectile_speed_returns_none():
    assert solve_intercept(ORIGIN, (10.0, 0.0), (0.0, 0.0), 0.0) is None
    assert solve_intercept(ORIGIN, (10.0, 0.0), (0.0, 0.0), -5.0) is None


def test_non_finite_input_returns_none():
    assert solve_intercept(ORIGIN, (float("nan"), 0.0), (0.0, 0.0), 50.0) is None
    assert solve_intercept(ORIGIN, (10.0, 0.0), (float("inf"), 0.0), 50.0) is None
    assert solve_intercept((float("nan"), 0.0), (10.0, 0.0), (0.0, 0.0), 50.0) is None
    assert solve_intercept(ORIGIN, (10.0, 0.0), (0.0, 0.0), float("nan")) is None


def test_negative_discriminant_returns_none():
    # A target moving fast and roughly perpendicular relative to a slower
    # projectile, positioned so no real intercept geometry exists.
    solution = solve_intercept(ORIGIN, (5.0, 0.0), (0.0, 1000.0), 10.0)
    assert solution is None
