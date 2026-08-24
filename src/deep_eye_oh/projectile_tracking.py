"""Own-projectile correlation and dynamic projectile-speed estimation.

Renderer ownership of a generic circle observation (BrowserCircle -- see
browser_game_state.py) is never assumed: the Oracle reports circles with no
entity ID, no "enemy"/"own" field, and no color contract (see
deep.eye.oh.ext's oracle.js circles()). This module infers LIKELY own
projectiles purely by correlating circle observations against things this
process genuinely knows -- our own shoot action being active, our own
self-position proxy, and our own commanded aim direction -- never by
trusting anything the Oracle itself claims about ownership.

Two responsibilities live here, deliberately kept as the smallest tracker
that can produce the thing the intercept solver actually needs (never a
general entity tracker -- see CLAUDE.md's "small vertical slices"):

  OwnProjectileTracker: a minimal, ambiguity-averse multi-track correlator
  that turns a rolling stream of BrowserCircle observations into
  ProjectileSpeedSample events (one per newly-confirmed consecutive pair of
  observations on the same likely-own-projectile track).

  ProjectileSpeedEstimator: maintains a single adaptive
  ProjectileSpeedEstimate from those samples using robust statistics, with
  explicit regime-shift handling so a mid-session Bullet Speed upgrade is
  tracked rather than permanently averaged away. Effective projectile speed
  is a function of the player's current build, NOT tank class or any other
  static property -- there is deliberately no DIEP_BULLET_SPEED constant
  anywhere in this codebase; see the projectile-speed-and-lead-v0 PR
  description for why.
"""

from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from deep_eye_oh.browser_game_state import BrowserCircle

# --- Own-projectile correlation --------------------------------------------

# A new track may only be seeded from a circle within this distance of the
# self-position proxy (canvas center -- see browser_farming._canvas_origin)
# -- a muzzle-adjacent zone, not a tight point, because the actual muzzle
# offset from tank center is unknown without live evidence and the first
# post-fire observation may already be a frame or two of travel away.
MUZZLE_RADIUS_PX = 80.0

# A new track's initial displacement from self must be within roughly this
# many degrees of the commanded aim direction (cos(60 deg) = 0.5) -- own
# projectiles travel toward where we aimed; anything wildly off-axis is not
# plausibly ours.
MIN_AIM_ALIGNMENT_COSINE = 0.5

# How far (px) an existing track's predicted next position may be from a
# candidate observation for that observation to be considered a match at
# all. Generous but bounded: a real projectile at a plausible upper-bound
# speed (~2000 px/s) over one bridge tick (~50ms) travels ~100px; this
# allows headroom for tick jitter without accepting an unrelated circle.
MAX_ASSOCIATION_JUMP_PX = 260.0

# If a second candidate is within this margin of the best candidate's
# distance, the match is ambiguous -- dropped rather than guessed (see
# module docstring: "a track that becomes ambiguous should be dropped or
# lose confidence rather than arbitrarily reassigned").
ASSOCIATION_AMBIGUITY_MARGIN_PX = 20.0

# Fourth live-smoke finding: two candidates a few px apart in POSITION but
# only a fraction of a millisecond apart in TIMESTAMP -- from sub-
# millisecond browser clock precision on genuinely distinct nearby
# entities, not one smoothly-moving entity -- implied an absurd speed
# (tens/hundreds of THOUSANDS of px/s) once accepted as an "echo" purely
# on spatial closeness. A real physical entity's frame-to-frame motion
# cannot imply a speed beyond this generous ceiling (comfortably above
# any plausible bullet, even at extreme upgrades); two spatially-close
# candidates whose IMPLIED speed exceeds it are not temporal echoes of
# one entity and must fall through to the ordinary ambiguity rejection,
# not be merged.
MAX_PLAUSIBLE_ECHO_SPEED_PX_S = 3000.0

INITIAL_TRACK_CONFIDENCE = 0.34
TRACK_CONFIDENCE_STEP_UP = 0.22
TRACK_CONFIDENCE_STEP_DOWN = 0.34
MIN_CONFIDENCE_TO_EMIT_SAMPLE = 0.5

# How many recent positions/timestamps a track retains -- only the most
# recent two are ever used for a velocity sample; a small amount of extra
# history is kept only so a future consumer could sanity-check size/speed
# consistency across more than one step without unbounded growth.
MAX_TRACK_HISTORY = 8

MAX_TRACK_COUNT = 8  # bounds total memory; diep.io fire rate makes this generous

# Third live-smoke finding: MUZZLE_RADIUS_PX + aim-alignment alone is not
# bullet-specific enough -- any nearby, modestly-moving entity (another
# tank in melee range, roughly in the direction being fought) can satisfy
# both and get seeded as a track too. Live evidence showed exactly this: a
# single ProjectileSpeedEstimator window mixing genuine ~260-470 px/s
# samples with implausibly slow ~9-180 px/s ones, driving
# bullet_speed_confidence to 0.00 every time (dispersion far exceeds
# MAX_ACCEPTABLE_RELATIVE_DISPERSION) despite a full 20-sample window. A
# projectile is categorically faster than any tank's movement speed, so a
# conservative floor on the OBSERVED per-step speed -- not a claim about
# what bullet speed IS, just a sanity bound below which something is
# definitely not a projectile -- is the right discriminator, applied
# before a sample ever reaches the estimator (rather than trying to
# untangle a bimodal mixture statistically downstream). v0 heuristic set
# from this live evidence's clear gap between the two clusters; refine
# further if live evidence warrants.
MIN_PLAUSIBLE_PROJECTILE_SPEED_PX_S = 200.0


@dataclass(frozen=True)
class ProjectileSpeedSample:
    """One observed own-projectile speed measurement, from two consecutive
    correlated observations of the same likely-own-projectile track."""

    speed_px_s: float
    measured_at_ms: float


@dataclass
class _Track:
    positions: list[tuple[float, float]]
    timestamps_ms: list[float]
    confidence: float


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _is_plausible_echo(circle: BrowserCircle, other: BrowserCircle) -> bool:
    """Is `circle` plausibly another recent-frame observation of the SAME
    entity as `other` -- different timestamp, close in position, AND an
    implied speed between them that isn't physically absurd (see
    MAX_PLAUSIBLE_ECHO_SPEED_PX_S's module comment)? A tiny timestamp gap
    with a non-trivial position gap implies an impossible speed and is
    NOT an echo, regardless of how spatially close the two are."""
    if circle.timestamp_ms == other.timestamp_ms:
        return False
    spatial_distance = _distance((circle.cx, circle.cy), (other.cx, other.cy))
    if spatial_distance > ASSOCIATION_AMBIGUITY_MARGIN_PX:
        return False
    dt_s = abs(circle.timestamp_ms - other.timestamp_ms) / 1000.0
    implied_speed = spatial_distance / dt_s if dt_s > 0 else math.inf
    return implied_speed <= MAX_PLAUSIBLE_ECHO_SPEED_PX_S


def _predict_next_position(track: _Track) -> tuple[float, float]:
    """Simple constant-velocity one-step extrapolation from the track's two
    most recent samples (or just its last known position, if it only has
    one so far)."""
    if len(track.positions) < 2:
        return track.positions[-1]
    (x0, y0), (x1, y1) = track.positions[-2], track.positions[-1]
    dt = track.timestamps_ms[-1] - track.timestamps_ms[-2]
    if not (dt > 0):
        return track.positions[-1]
    vx = (x1 - x0) / dt
    vy = (y1 - y0) / dt
    return (x1 + (vx * dt), y1 + (vy * dt))


def _best_match(
    predicted: tuple[float, float],
    circles: Sequence[BrowserCircle],
    claimed: set[int],
) -> tuple[int, BrowserCircle] | None:
    """Nearest unclaimed circle to `predicted`, within
    MAX_ASSOCIATION_JUMP_PX -- or None.

    Live evidence (projectile-speed-and-lead-v0's live smoke) showed the
    Oracle's circle cache commonly delivers SEVERAL recent-frame echoes of
    the SAME physical entity within one tick's circle list -- consecutive
    bridge polls only ~100ms apart against a ~250ms cache window overlap
    substantially, so one moving tank can appear 2-3 times a couple
    pixels apart, each from a slightly different recent frame, not from
    two different entities. Naively rejecting any two comparably-close
    candidates as "ambiguous" (the original design) meant this happened
    on almost every real tick, and no track ever survived past its first
    observation. The distinguishing signal is: genuinely competing
    candidates (two different entities that happen to be near each
    other) are NOT close to EACH OTHER, only individually close to
    `predicted` -- multiple echoes of one entity ARE close to each other
    too. So: among candidates comparably close to the current best (within
    ASSOCIATION_AMBIGUITY_MARGIN_PX of its distance), one that is ALSO
    within that margin of the current best's OWN position is treated as
    another echo of the same entity (pick whichever is fresher by
    timestamp, never ambiguous); a candidate that is comparably close to
    `predicted` but NOT close to the current best's position is a
    genuinely distinct, competing hypothesis -- fail closed there, do not
    guess which one is real.
    """
    candidates = []
    for index, circle in enumerate(circles):
        if index in claimed:
            continue
        distance = _distance(predicted, (circle.cx, circle.cy))
        if distance <= MAX_ASSOCIATION_JUMP_PX:
            candidates.append((index, circle, distance))
    if not candidates:
        return None

    candidates.sort(key=lambda item: item[2])
    best_index, best_circle, best_distance = candidates[0]
    for index, circle, distance in candidates[1:]:
        if distance - best_distance >= ASSOCIATION_AMBIGUITY_MARGIN_PX:
            break  # sorted by distance -- everything after this is even farther
        if _is_plausible_echo(circle, best_circle):
            # A different-TIME echo of the same entity (see this
            # function's docstring), not a competing one -- prefer
            # whichever observation is more recent. Two candidates at the
            # exact same timestamp, or whose implied speed is physically
            # absurd, are NOT an echo (see _is_plausible_echo) and fall
            # through to the ambiguous-reject below.
            if circle.timestamp_ms > best_circle.timestamp_ms:
                best_index, best_circle, best_distance = index, circle, distance
            continue
        return None  # a genuinely distinct, comparably-plausible candidate -- do not guess

    return best_index, best_circle


class OwnProjectileTracker:
    """Correlates recent BrowserCircle observations into likely-own-
    projectile tracks and emits a ProjectileSpeedSample for each newly
    confirmed consecutive pair on a sufficiently-confident track.

    Call update() once per tick with the CURRENT full set of recent circle
    observations (e.g. state.circles), the current self-position proxy, the
    current commanded aim direction (a unit-ish vector; only its direction
    matters), and whether the shoot action is currently active. Stateful
    across calls -- one instance per farming-loop session.
    """

    def __init__(self) -> None:
        self._tracks: list[_Track] = []
        self._claimed_positions: frozenset[tuple[float, float]] = frozenset()

    def update(
        self,
        circles: Sequence[BrowserCircle],
        *,
        now_ms: float,
        self_position: tuple[float, float] | None,
        aim_direction: tuple[float, float] | None,
        shoot_active: bool,
    ) -> list[ProjectileSpeedSample]:
        claimed: set[int] = set()

        # 1. Advance existing tracks against this tick's observations.
        for track in self._tracks:
            match = _best_match(_predict_next_position(track), circles, claimed)
            if match is None:
                track.confidence -= TRACK_CONFIDENCE_STEP_DOWN
                continue
            index, circle = match
            claimed.add(index)
            track.positions.append((circle.cx, circle.cy))
            track.timestamps_ms.append(circle.timestamp_ms)
            if len(track.positions) > MAX_TRACK_HISTORY:
                track.positions.pop(0)
                track.timestamps_ms.pop(0)
            track.confidence = min(1.0, track.confidence + TRACK_CONFIDENCE_STEP_UP)

        self._tracks = [t for t in self._tracks if t.confidence > 0]

        # 2. Seed new tracks from unclaimed circles, only while shooting and
        # only near self, aligned with where we are currently aiming.
        if shoot_active and self_position is not None and aim_direction is not None:
            aim_norm = math.hypot(aim_direction[0], aim_direction[1])
            if aim_norm > 0:
                for index, circle in enumerate(circles):
                    if index in claimed or len(self._tracks) >= MAX_TRACK_COUNT:
                        continue
                    dx = circle.cx - self_position[0]
                    dy = circle.cy - self_position[1]
                    dist = math.hypot(dx, dy)
                    if not (0 < dist <= MUZZLE_RADIUS_PX):
                        continue
                    cosine = ((dx * aim_direction[0]) + (dy * aim_direction[1])) / (dist * aim_norm)
                    if cosine < MIN_AIM_ALIGNMENT_COSINE:
                        continue
                    self._tracks.append(_Track(
                        positions=[(circle.cx, circle.cy)],
                        timestamps_ms=[circle.timestamp_ms],
                        confidence=INITIAL_TRACK_CONFIDENCE,
                    ))
                    claimed.add(index)

        self._claimed_positions = frozenset((circles[i].cx, circles[i].cy) for i in claimed)

        # 3. Emit one speed sample per track that just gained a confirmed
        # consecutive pair and has crossed the confidence bar.
        samples: list[ProjectileSpeedSample] = []
        for track in self._tracks:
            if len(track.positions) < 2 or track.confidence < MIN_CONFIDENCE_TO_EMIT_SAMPLE:
                continue
            (x0, y0), (x1, y1) = track.positions[-2], track.positions[-1]
            dt_ms = track.timestamps_ms[-1] - track.timestamps_ms[-2]
            if not (dt_ms > 0):
                continue
            speed_px_s = _distance((x0, y0), (x1, y1)) / (dt_ms / 1000.0)

            # An incoming enemy bullet, just before it reaches us, can
            # transiently satisfy the muzzle/aim-alignment seed criteria
            # too (near self, roughly toward whoever we're both facing) --
            # but unlike our own outgoing shots, its distance from self is
            # DECREASING, not increasing. A genuine own projectile always
            # moves away from self by definition; require that here as an
            # additional discriminator, independent of speed.
            if self_position is not None:
                distance_before = _distance((x0, y0), self_position)
                distance_after = _distance((x1, y1), self_position)
                if not (distance_after > distance_before):
                    continue

            # Lower bound (MIN_PLAUSIBLE_PROJECTILE_SPEED_PX_S): an
            # implausibly slow "projectile" is almost certainly a nearby
            # tank, not a bullet. Upper bound (MAX_PLAUSIBLE_ECHO_SPEED_PX_S,
            # reused here as a general physical-plausibility ceiling, not
            # just an echo-matching concept): defense in depth against a
            # tiny dt_ms slipping through match resolution some other way
            # and implying an impossible speed -- see that constant's
            # module comment for the live evidence (speeds in the tens/
            # hundreds of THOUSANDS of px/s). Either way, drop the
            # observation rather than let it corrupt the estimator.
            if (
                math.isfinite(speed_px_s)
                and MIN_PLAUSIBLE_PROJECTILE_SPEED_PX_S <= speed_px_s <= MAX_PLAUSIBLE_ECHO_SPEED_PX_S
            ):
                samples.append(ProjectileSpeedSample(speed_px_s=speed_px_s, measured_at_ms=track.timestamps_ms[-1]))

        return samples

    @property
    def active_track_count(self) -> int:
        return len(self._tracks)

    @property
    def claimed_positions(self) -> frozenset[tuple[float, float]]:
        """(cx, cy) of every circle claimed (matched to an existing track,
        or used to seed a new one) by the MOST RECENT update() call --
        callers (browser_farming.py) use this to exclude likely-own-
        projectile observations from moving-target candidate selection."""
        return self._claimed_positions


# --- Dynamic projectile-speed estimation -----------------------------------

DEFAULT_WINDOW_SIZE = 20
DEFAULT_SAMPLE_MAX_AGE_S = 5.0
MIN_SAMPLES_FOR_ESTIMATE = 3
MIN_SAMPLES_FOR_TRIMMED_MEAN = 5
TRIM_FRACTION = 0.1  # drop the top/bottom 10% as outliers once there is enough data
REGIME_SHIFT_RECENT_N = 5
REGIME_SHIFT_RELATIVE_DELTA = 0.25  # a >25% jump between recent and overall medians is treated as a build change
# Live-smoke calibration: with the outgoing-direction filter above (and
# the earlier echo/speed-plausibility fixes), observed own-projectile
# samples were no longer buggy, but still showed genuine relative
# dispersion in roughly the 15-30% range within a single window -- likely
# real camera-relative measurement noise (our own tank's WASD movement
# shifts the camera, and thus a bullet's OBSERVED canvas-space speed,
# independent of the bullet's own true speed) rather than a bug. The
# original 0.3 cap left confidence pinned near 0 for nearly all of a live
# session despite individually-sane speed values; widened from this
# session's own observed dispersion range so a realistic, non-buggy
# window can actually cross compute_lead's confidence bar.
MAX_ACCEPTABLE_RELATIVE_DISPERSION = 0.5


def _trimmed_mean(values: Sequence[float]) -> float:
    ordered = sorted(values)
    trim = int(len(ordered) * TRIM_FRACTION)
    trimmed = ordered[trim: len(ordered) - trim] if trim > 0 else ordered
    return statistics.fmean(trimmed) if trimmed else statistics.fmean(ordered)


@dataclass(frozen=True)
class ProjectileSpeedEstimate:
    """speed_px_s/confidence are None/0.0 (never a guess) when there is not
    yet enough fresh, consistent evidence -- see ProjectileSpeedEstimator's
    docstring for exactly when."""

    speed_px_s: float | None
    confidence: float
    sample_count: int
    measured_at: float
    last_updated: float | None

    @property
    def available(self) -> bool:
        return self.speed_px_s is not None


class ProjectileSpeedEstimator:
    """Adaptive estimate of the player's CURRENT effective projectile speed
    from recent ProjectileSpeedSample events. Never a static/class-based
    constant -- see module docstring.

    Design (see the projectile-speed-and-lead-v0 PR description for the
    rationale): a rolling recent-sample window, a robust central estimate
    (trimmed mean once there are enough samples, else plain median),
    confidence from sample count + dispersion + freshness together, and
    explicit regime-shift handling -- if the most recent few samples
    persistently disagree with the established median by more than
    REGIME_SHIFT_RELATIVE_DELTA, older samples are dropped fast rather than
    blended into a stale average (a Bullet Speed upgrade can happen
    mid-session; this is never detected directly, only inferred from the
    samples themselves).
    """

    def __init__(
        self,
        *,
        window_size: int = DEFAULT_WINDOW_SIZE,
        sample_max_age_s: float = DEFAULT_SAMPLE_MAX_AGE_S,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_size = window_size
        self._sample_max_age_s = sample_max_age_s
        self._time_source = time_source
        self._samples: list[tuple[float, float]] = []  # (speed_px_s, monotonic time added)

    def add_sample(self, speed_px_s: float, *, now: float | None = None) -> None:
        if not math.isfinite(speed_px_s) or speed_px_s <= 0:
            return
        now = now if now is not None else self._time_source()
        self._samples.append((speed_px_s, now))
        if len(self._samples) > self._window_size:
            self._samples.pop(0)

    def estimate(self, *, now: float | None = None) -> ProjectileSpeedEstimate:
        now = now if now is not None else self._time_source()
        fresh = [(speed, t) for speed, t in self._samples if (now - t) <= self._sample_max_age_s]

        if len(fresh) < MIN_SAMPLES_FOR_ESTIMATE:
            return ProjectileSpeedEstimate(
                speed_px_s=None, confidence=0.0, sample_count=len(fresh),
                measured_at=now, last_updated=fresh[-1][1] if fresh else None,
            )

        speeds = [speed for speed, _ in fresh]
        overall_median = statistics.median(speeds)
        recent = fresh[-REGIME_SHIFT_RECENT_N:]
        recent_speeds = [speed for speed, _ in recent]
        recent_median = statistics.median(recent_speeds)

        if (
            overall_median > 0
            and len(fresh) > len(recent)
            and abs(recent_median - overall_median) / overall_median > REGIME_SHIFT_RELATIVE_DELTA
        ):
            # A persistent shift: trust only the recent cluster, retire the
            # rest immediately rather than let them keep dragging the
            # estimate toward the old speed.
            fresh = recent
            speeds = recent_speeds

        estimate_speed = (
            _trimmed_mean(speeds) if len(speeds) >= MIN_SAMPLES_FOR_TRIMMED_MEAN else statistics.median(speeds)
        )

        dispersion = statistics.pstdev(speeds) if len(speeds) > 1 else 0.0
        relative_dispersion = (dispersion / estimate_speed) if estimate_speed > 0 else 1.0
        newest_age_s = now - fresh[-1][1]

        sample_confidence = min(1.0, len(speeds) / self._window_size)
        dispersion_confidence = max(0.0, 1.0 - min(relative_dispersion / MAX_ACCEPTABLE_RELATIVE_DISPERSION, 1.0))
        freshness_confidence = max(0.0, 1.0 - min(newest_age_s / self._sample_max_age_s, 1.0))
        confidence = sample_confidence * dispersion_confidence * freshness_confidence

        return ProjectileSpeedEstimate(
            speed_px_s=estimate_speed,
            confidence=confidence,
            sample_count=len(speeds),
            measured_at=now,
            last_updated=fresh[-1][1],
        )
