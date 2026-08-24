"""Minimal single-target motion tracking.

This exists to prove lead (projectile-speed-and-lead-v0), not to build a
combat perception stack: exactly one target is tracked at a time, via
simple one-to-one nearest-neighbor temporal association between
consecutive candidate observations -- see CLAUDE.md's "small vertical
slices" and this milestone's explicit "do NOT build general multi-object
tracking infrastructure".

Callers (browser_farming.py) are responsible for choosing WHICH generic
circle observations are even offered as candidates each tick (excluding
self, excluding anything currently claimed by OwnProjectileTracker as a
likely own projectile -- see projectile_tracking.py) -- this module only
tracks whichever single candidate stream it is given, using real observed
timestamps (never assuming a fixed bridge polling frequency) for velocity.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

DEFAULT_MAX_JUMP_PX = 320.0
ASSOCIATION_AMBIGUITY_MARGIN_PX = 20.0
DEFAULT_MIN_CONFIDENCE = 0.5

INITIAL_CONFIDENCE = 0.34
CONFIDENCE_STEP_UP = 0.22
CONFIDENCE_STEP_DOWN = 0.34


@dataclass(frozen=True)
class TargetCandidate:
    """One candidate observation a caller offers to TargetTracker.update()
    this tick -- deliberately the same shape as BrowserCircle's positional
    fields, but a distinct type: this module has no dependency on
    browser_game_state, so it stays usable from a future screen-only
    StateEstimator too (see CLAUDE.md)."""

    cx: float
    cy: float
    radius: float
    timestamp_ms: float


@dataclass(frozen=True)
class TargetObservation:
    """The tracker's current belief about the target: position, velocity
    (real px/s, derived from actual observed timestamps), and confidence.
    Only returned once at least two consistent observations have been
    associated and confidence has crossed the tracker's threshold -- never
    a guess from a single sighting."""

    cx: float
    cy: float
    vx: float
    vy: float
    radius: float
    timestamp_ms: float
    confidence: float

    @property
    def position(self) -> tuple[float, float]:
        return (self.cx, self.cy)

    @property
    def velocity(self) -> tuple[float, float]:
        return (self.vx, self.vy)

    @property
    def speed_px_s(self) -> float:
        return math.hypot(self.vx, self.vy)


def _distance(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


class TargetTracker:
    """Tracks exactly one target across ticks. update() must be called
    once per tick (even with an empty candidate list) so confidence decay
    reflects real elapsed observation gaps."""

    def __init__(
        self,
        *,
        max_jump_px: float = DEFAULT_MAX_JUMP_PX,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ) -> None:
        self._max_jump_px = max_jump_px
        self._min_confidence = min_confidence
        self._last: TargetCandidate | None = None
        self._velocity: tuple[float, float] = (0.0, 0.0)
        self._confidence: float = 0.0

    def update(self, candidates: Sequence[TargetCandidate], now_ms: float) -> TargetObservation | None:
        if self._last is None:
            seed = candidates[0] if candidates else None
            if seed is not None:
                self._last = seed
                self._confidence = INITIAL_CONFIDENCE
            return None  # a single sighting has no velocity yet

        match = self._best_match(candidates)
        if match is None:
            self._confidence -= CONFIDENCE_STEP_DOWN
            if self._confidence <= 0:
                self._last = None
                self._velocity = (0.0, 0.0)
            return None

        dt_ms = match.timestamp_ms - self._last.timestamp_ms
        if not (dt_ms > 0):
            # A stale, duplicate, or out-of-order sample -- ignore rather
            # than divide by zero/negative or corrupt the velocity estimate.
            return None

        vx = (match.cx - self._last.cx) / (dt_ms / 1000.0)
        vy = (match.cy - self._last.cy) / (dt_ms / 1000.0)
        self._velocity = (vx, vy)
        self._last = match
        self._confidence = min(1.0, self._confidence + CONFIDENCE_STEP_UP)

        if self._confidence < self._min_confidence:
            return None

        return TargetObservation(
            cx=match.cx, cy=match.cy, vx=vx, vy=vy, radius=match.radius,
            timestamp_ms=match.timestamp_ms, confidence=self._confidence,
        )

    def _best_match(self, candidates: Sequence[TargetCandidate]) -> TargetCandidate | None:
        assert self._last is not None
        best: TargetCandidate | None = None
        best_distance: float | None = None
        second_best_distance: float | None = None
        for candidate in candidates:
            distance = _distance(candidate.cx, candidate.cy, self._last.cx, self._last.cy)
            if distance > self._max_jump_px:
                continue  # implausible jump -- not a plausible continuation
            if best_distance is None or distance < best_distance:
                second_best_distance = best_distance
                best_distance = distance
                best = candidate
            elif second_best_distance is None or distance < second_best_distance:
                second_best_distance = distance
        if best is None:
            return None
        if (
            second_best_distance is not None
            and (second_best_distance - best_distance) < ASSOCIATION_AMBIGUITY_MARGIN_PX
        ):
            return None  # ambiguous -- do not guess which candidate continues the track
        return best

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def has_target(self) -> bool:
        return self._last is not None
