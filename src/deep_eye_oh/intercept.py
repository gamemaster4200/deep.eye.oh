"""Pure 2D intercept geometry.

Given a shooter position, a target position, a target velocity, and a
projectile speed, solve for the minimum positive time to intercept and the
resulting aim point. This module knows nothing about Browser Oracle,
Controller, tanks, projectiles, or any other deep_eye_oh concept -- it is
pure vector algebra, testable in complete isolation (see CLAUDE.md's
constraint that Policy-adjacent pure logic not depend on any concrete
Environment).

Physics: a projectile fired now at speed u reaches the target's future
position p + v*t exactly when |p + v*t - s| = u*t, i.e. the projectile
(traveling at constant speed u in a straight line from s) and the target
(traveling at constant velocity v from p) arrive at the same point at the
same time t. Squaring and expanding gives the quadratic

    (v.v - u^2) t^2 + 2*((p-s).v) t + |p-s|^2 = 0

solved below for the minimum physically valid (finite, positive, real) t.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

_ZERO_COEFFICIENT_EPS = 1e-9
_MIN_POSITIVE_T = 1e-9


@dataclass(frozen=True)
class InterceptSolution:
    t: float
    aim_x: float
    aim_y: float


def solve_intercept(
    shooter: tuple[float, float],
    target: tuple[float, float],
    target_velocity: tuple[float, float],
    projectile_speed: float,
) -> InterceptSolution | None:
    """Returns the minimum positive-time intercept solution, or None if
    none exists (fail closed -- never a guess). None is returned for:
    non-finite input; projectile_speed <= 0; the near-zero leading
    ("linear degeneracy") case with no positive root; a negative
    discriminant (no real intercept geometry); and the ordinary quadratic
    case with no positive root (e.g. an unreachable receding target).
    """
    sx, sy = shooter
    px, py = target
    vx, vy = target_velocity

    for value in (sx, sy, px, py, vx, vy, projectile_speed):
        if not math.isfinite(value):
            return None
    if projectile_speed <= 0:
        return None

    # Target position relative to the shooter -- the quadratic below is
    # expressed entirely in this relative frame.
    rx = px - sx
    ry = py - sy

    a = (vx * vx) + (vy * vy) - (projectile_speed * projectile_speed)
    b = 2.0 * ((rx * vx) + (ry * vy))
    c = (rx * rx) + (ry * ry)

    if abs(a) < _ZERO_COEFFICIENT_EPS:
        # |target velocity| ~= projectile_speed: the t^2 term vanishes and
        # this is a linear equation, b*t + c = 0.
        if abs(b) < _ZERO_COEFFICIENT_EPS:
            # No t dependence left either -- either no solution (c != 0)
            # or every t is a "solution" (c == 0, target already at the
            # shooter's position with exactly matching speed): neither is
            # a single well-defined minimum positive t, so fail closed.
            return None
        t = -c / b
        if not math.isfinite(t) or t <= _MIN_POSITIVE_T:
            return None
    else:
        discriminant = (b * b) - (4.0 * a * c)
        if discriminant < 0:
            return None
        sqrt_discriminant = math.sqrt(discriminant)
        t1 = (-b - sqrt_discriminant) / (2.0 * a)
        t2 = (-b + sqrt_discriminant) / (2.0 * a)
        candidates = [
            candidate for candidate in (t1, t2)
            if math.isfinite(candidate) and candidate > _MIN_POSITIVE_T
        ]
        if not candidates:
            return None
        t = min(candidates)

    aim_x = px + (vx * t)
    aim_y = py + (vy * t)
    if not (math.isfinite(aim_x) and math.isfinite(aim_y)):
        return None

    return InterceptSolution(t=t, aim_x=aim_x, aim_y=aim_y)
