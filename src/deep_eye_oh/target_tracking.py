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

# Live-smoke finding (see projectile_tracking.py's identical constant for
# the full story): two candidates a few px apart in position but only a
# fraction of a millisecond apart in timestamp implied an absurd speed
# once accepted as an echo purely on spatial closeness. A real target's
# frame-to-frame motion cannot imply a speed beyond this generous
# ceiling; two spatially-close candidates whose implied speed exceeds it
# are not echoes of one entity and must fall through to the ordinary
# ambiguity rejection.
MAX_PLAUSIBLE_ECHO_SPEED_PX_S = 3000.0

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


def _is_plausible_echo(candidate: TargetCandidate, other: TargetCandidate) -> bool:
    """Is `candidate` plausibly another recent-frame observation of the
    SAME entity as `other` -- different timestamp, close in position, AND
    an implied speed between them that isn't physically absurd (see
    MAX_PLAUSIBLE_ECHO_SPEED_PX_S)? A tiny timestamp gap with a non-
    trivial position gap implies an impossible speed and is NOT an echo,
    regardless of how spatially close the two are."""
    if candidate.timestamp_ms == other.timestamp_ms:
        return False
    spatial_distance = _distance(candidate.cx, candidate.cy, other.cx, other.cy)
    if spatial_distance > ASSOCIATION_AMBIGUITY_MARGIN_PX:
        return False
    dt_s = abs(candidate.timestamp_ms - other.timestamp_ms) / 1000.0
    implied_speed = spatial_distance / dt_s if dt_s > 0 else math.inf
    return implied_speed <= MAX_PLAUSIBLE_ECHO_SPEED_PX_S


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
        """Nearest candidate to the tracked target's last known position,
        within max_jump_px -- or None.

        Live evidence (projectile-speed-and-lead-v0's live smoke) showed
        the Oracle's circle cache commonly delivers SEVERAL recent-frame
        echoes of the SAME physical entity within one tick's candidate
        list (consecutive bridge polls ~100ms apart against a ~250ms
        cache window overlap substantially). Naively rejecting any two
        comparably-close candidates as "ambiguous" meant a real target
        was rejected almost every tick, and no track ever survived past
        its first sighting. The distinguishing signal: genuinely
        competing candidates (two different entities that happen to be
        near each other) are NOT close to EACH OTHER, only individually
        close to the tracked position -- multiple echoes of one entity
        ARE close to each other too. Among candidates comparably close to
        the current best (within ASSOCIATION_AMBIGUITY_MARGIN_PX of its
        distance), one that is ALSO within that margin of the current
        best's OWN position is treated as another echo of the same
        entity (prefer whichever is fresher by timestamp, never
        ambiguous); one that is comparably close but NOT close to the
        current best's position is a genuinely distinct, competing
        hypothesis -- fail closed there, do not guess.
        """
        assert self._last is not None
        candidates_with_distance = []
        for candidate in candidates:
            distance = _distance(candidate.cx, candidate.cy, self._last.cx, self._last.cy)
            if distance <= self._max_jump_px:
                candidates_with_distance.append((candidate, distance))
        if not candidates_with_distance:
            return None

        candidates_with_distance.sort(key=lambda item: item[1])
        best, best_distance = candidates_with_distance[0]
        for candidate, distance in candidates_with_distance[1:]:
            if distance - best_distance >= ASSOCIATION_AMBIGUITY_MARGIN_PX:
                break  # sorted by distance -- everything after this is even farther
            if _is_plausible_echo(candidate, best):
                # A different-TIME echo of the same entity, not a
                # competing one -- prefer whichever is more recent. Same
                # timestamp, or a physically-absurd implied speed, is NOT
                # an echo (see _is_plausible_echo) and falls through to
                # the reject below.
                if candidate.timestamp_ms > best.timestamp_ms:
                    best, best_distance = candidate, distance
                continue
            return None  # a genuinely distinct, comparably-plausible candidate -- do not guess

        return best

    @property
    def confidence(self) -> float:
        return self._confidence

    @property
    def has_target(self) -> bool:
        return self._last is not None
