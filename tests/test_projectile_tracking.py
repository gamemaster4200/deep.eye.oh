"""Tests for projectile_tracking.py: own-projectile correlation
(OwnProjectileTracker) and adaptive speed estimation
(ProjectileSpeedEstimator)."""

import math

from deep_eye_oh.browser_game_state import BrowserCircle
from deep_eye_oh.projectile_tracking import (
    MAX_PLAUSIBLE_ECHO_SPEED_PX_S,
    MIN_PLAUSIBLE_PROJECTILE_SPEED_PX_S,
    MUZZLE_RADIUS_PX,
    OwnProjectileTracker,
    ProjectileSpeedEstimator,
)

SELF = (500.0, 500.0)
AIM_RIGHT = (1.0, 0.0)


def _circle(cx, cy, timestamp_ms, radius=3.0, color=None):
    return BrowserCircle(cx=cx, cy=cy, radius=radius, color=color, timestamp_ms=timestamp_ms)


# ---------------------------------------------------------------------------
# OwnProjectileTracker: correlation
# ---------------------------------------------------------------------------


def test_no_samples_when_not_shooting():
    tracker = OwnProjectileTracker()
    circles = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    samples = tracker.update(circles, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=False)
    assert samples == []
    assert tracker.active_track_count == 0


def test_no_track_seeded_far_from_self():
    tracker = OwnProjectileTracker()
    far_circle = [_circle(SELF[0] + MUZZLE_RADIUS_PX + 100, SELF[1], 0.0)]
    tracker.update(far_circle, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.active_track_count == 0


def test_no_track_seeded_misaligned_with_aim():
    tracker = OwnProjectileTracker()
    # Directly behind self relative to aim direction (aiming +x, circle at -x).
    behind = [_circle(SELF[0] - 20, SELF[1], 0.0)]
    tracker.update(behind, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.active_track_count == 0


def test_track_seeded_near_self_aligned_with_aim():
    tracker = OwnProjectileTracker()
    near_aligned = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    tracker.update(near_aligned, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.active_track_count == 1


def test_consecutive_matches_emit_speed_sample_with_correct_magnitude():
    tracker = OwnProjectileTracker()
    # Own projectile fired to the right at 700 px/s: at t=0 it is 20px from
    # self (already traveling), at t=0.05s it has moved 35px further.
    step1 = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    samples1 = tracker.update(step1, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert samples1 == [], "a single observation cannot yet produce a speed sample"

    step2 = [_circle(SELF[0] + 55, SELF[1], 50.0)]  # +35px over 50ms = 700 px/s
    samples2 = tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert len(samples2) == 1
    assert math.isclose(samples2[0].speed_px_s, 700.0, rel_tol=1e-6)
    assert samples2[0].measured_at_ms == 50.0


def test_unrelated_circle_elsewhere_does_not_corrupt_track():
    tracker = OwnProjectileTracker()
    step1 = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    tracker.update(step1, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)

    # An unrelated circle far away (e.g. a neutral-shape-adjacent render, or
    # another player's shot) must not be picked up by our track, and must
    # not itself start a new track (too far from self / not shooting-aligned
    # is irrelevant here -- it's simply far from the predicted continuation).
    step2 = [
        _circle(SELF[0] + 55, SELF[1], 50.0),  # the real continuation
        _circle(SELF[0] + 55, SELF[1] + 900, 50.0),  # unrelated, far away
    ]
    samples = tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert len(samples) == 1
    assert math.isclose(samples[0].speed_px_s, 700.0, rel_tol=1e-6)


def test_multiple_recent_frame_echoes_of_the_same_entity_resolve_to_the_freshest():
    # Live-smoke regression: the Oracle's circle cache commonly delivers
    # SEVERAL recent-frame echoes of the SAME physical entity within one
    # tick's circle list (consecutive bridge polls ~100ms apart against a
    # ~250ms cache window overlap). These are NOT competing hypotheses --
    # naively rejecting them as ambiguous meant no track ever survived
    # past its first observation in the live run. Different timestamps,
    # close together spatially: resolve to the freshest, not ambiguous.
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 20, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)

    # Three echoes of the same moving projectile, each from a slightly
    # different recent frame (different timestamps), a couple pixels
    # apart -- exactly the live-observed pattern.
    step2 = [
        _circle(SELF[0] + 33, SELF[1], 40.0),
        _circle(SELF[0] + 35, SELF[1], 45.0),
        _circle(SELF[0] + 37, SELF[1], 50.0),  # freshest
    ]
    samples = tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert len(samples) == 1, "echoes of one entity must resolve to a match, not be rejected as ambiguous"
    # Matched against the FRESHEST echo (37px away, t=50.0): 17px over 50ms.
    assert math.isclose(samples[0].speed_px_s, 340.0, rel_tol=1e-6)
    assert samples[0].measured_at_ms == 50.0


def test_two_candidates_at_the_exact_same_timestamp_are_still_ambiguous():
    # Two comparably-close candidates at the SAME instant are a genuine
    # simultaneous ambiguity (two different real objects), not an echo of
    # one entity -- must still be rejected, not arbitrarily resolved.
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 20, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)

    step2 = [
        _circle(SELF[0] + 25, SELF[1] + 3, 50.0),
        _circle(SELF[0] + 25, SELF[1] - 3, 50.0),
    ]
    samples = tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert samples == []


def test_ambiguous_match_drops_confidence_rather_than_guessing():
    tracker = OwnProjectileTracker()
    step1 = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    tracker.update(step1, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)

    # Two candidates nearly equidistant from the predicted continuation
    # (predicted ~= (520, 500) with no prior velocity yet, so predicted is
    # just the last known position (520, 500)) -- ambiguous, must not guess.
    step2 = [
        _circle(SELF[0] + 25, SELF[1] + 3, 50.0),
        _circle(SELF[0] + 25, SELF[1] - 3, 50.0),
    ]
    samples = tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert samples == [], "an ambiguous association must not produce a confident speed sample"


def test_track_confidence_decays_and_track_is_dropped_when_observations_stop():
    tracker = OwnProjectileTracker()
    step1 = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    tracker.update(step1, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.active_track_count == 1

    # No matching circle for several consecutive ticks -- confidence must
    # decay to zero and the track must be dropped, not kept forever.
    for tick in range(1, 6):
        tracker.update([], now_ms=float(tick) * 50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.active_track_count == 0


def test_claimed_positions_reflects_most_recent_update_only():
    tracker = OwnProjectileTracker()
    step1 = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    tracker.update(step1, now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.claimed_positions == {(SELF[0] + 20, SELF[1])}

    step2 = [_circle(SELF[0] + 55, SELF[1], 50.0)]
    tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.claimed_positions == {(SELF[0] + 55, SELF[1])}, "must reflect only the latest tick, not accumulate"

    tracker.update([], now_ms=100.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=False)
    assert tracker.claimed_positions == frozenset()


def test_implausibly_slow_match_does_not_emit_a_speed_sample():
    # Live-smoke regression: a nearby, modestly-moving entity (e.g.
    # another tank in melee range) can satisfy the muzzle/aim-alignment
    # seed criteria too. Its slow observed speed must never reach the
    # estimator -- it corrupted bullet_speed_confidence to 0.00 in the
    # live run by mixing with genuine fast samples.
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 20, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    # Only 5px over 50ms = 100 px/s -- well under the plausibility floor.
    samples = tracker.update([_circle(SELF[0] + 25, SELF[1], 50.0)], now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert samples == []


def test_speed_exactly_at_the_plausibility_floor_is_accepted():
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 1.0, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    displacement = MIN_PLAUSIBLE_PROJECTILE_SPEED_PX_S * 0.05  # exactly the floor over 50ms
    samples = tracker.update(
        [_circle(SELF[0] + 1.0 + displacement, SELF[1], 50.0)], now_ms=50.0,
        self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True,
    )
    assert len(samples) == 1
    assert math.isclose(samples[0].speed_px_s, MIN_PLAUSIBLE_PROJECTILE_SPEED_PX_S, rel_tol=1e-6)


def test_tiny_time_gap_with_meaningful_position_gap_is_not_an_echo():
    # Live-smoke regression: two spatially-close candidates whose
    # timestamps differ by a fraction of a millisecond (sub-ms browser
    # clock precision on two genuinely distinct nearby entities) implied
    # an absurd speed (tens/hundreds of thousands of px/s) once accepted
    # as an "echo" purely on spatial closeness. Must be treated as a
    # genuine, unresolvable ambiguity instead -- not merged.
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 1, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)

    step2 = [
        _circle(SELF[0] + 15, SELF[1], 50.0),
        _circle(SELF[0] + 25, SELF[1], 50.001),  # 10px apart, 0.001ms apart -> ~10,000,000 px/s implied
    ]
    samples = tracker.update(step2, now_ms=50.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    assert samples == []


def test_implausibly_fast_unambiguous_match_does_not_emit_a_speed_sample():
    # Defense in depth: even a SINGLE, unambiguous candidate (no echo
    # resolution involved at all) must not produce a sample if the
    # implied speed between it and the track's prior point is physically
    # absurd -- covers any other path a tiny dt_ms could slip through.
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 1, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)

    # Sole candidate: 50px away, but only 0.01ms after the seed -> 5,000,000 px/s.
    samples = tracker.update(
        [_circle(SELF[0] + 51, SELF[1], 0.01)], now_ms=0.01,
        self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True,
    )
    assert samples == []


def test_speed_exactly_at_the_upper_plausibility_ceiling_is_accepted():
    tracker = OwnProjectileTracker()
    tracker.update([_circle(SELF[0] + 1, SELF[1], 0.0)], now_ms=0.0, self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True)
    displacement = MAX_PLAUSIBLE_ECHO_SPEED_PX_S * 0.05  # exactly the ceiling over 50ms
    samples = tracker.update(
        [_circle(SELF[0] + 1 + displacement, SELF[1], 50.0)], now_ms=50.0,
        self_position=SELF, aim_direction=AIM_RIGHT, shoot_active=True,
    )
    assert len(samples) == 1
    assert math.isclose(samples[0].speed_px_s, MAX_PLAUSIBLE_ECHO_SPEED_PX_S, rel_tol=1e-6)


def test_no_track_seeded_when_self_position_or_aim_unknown():
    tracker = OwnProjectileTracker()
    near = [_circle(SELF[0] + 20, SELF[1], 0.0)]
    tracker.update(near, now_ms=0.0, self_position=None, aim_direction=AIM_RIGHT, shoot_active=True)
    assert tracker.active_track_count == 0
    tracker.update(near, now_ms=0.0, self_position=SELF, aim_direction=None, shoot_active=True)
    assert tracker.active_track_count == 0


# ---------------------------------------------------------------------------
# ProjectileSpeedEstimator
# ---------------------------------------------------------------------------


def test_estimate_unavailable_with_insufficient_samples():
    estimator = ProjectileSpeedEstimator(time_source=lambda: 0.0)
    estimator.add_sample(700.0, now=0.0)
    estimator.add_sample(705.0, now=0.1)
    estimate = estimator.estimate(now=0.2)
    assert not estimate.available
    assert estimate.confidence == 0.0


def test_estimate_available_and_reasonable_with_consistent_samples():
    estimator = ProjectileSpeedEstimator(time_source=lambda: 0.0)
    for i, speed in enumerate([698.0, 701.0, 699.0, 702.0, 700.0]):
        estimator.add_sample(speed, now=i * 0.05)
    estimate = estimator.estimate(now=0.25)
    assert estimate.available
    assert math.isclose(estimate.speed_px_s, 700.0, abs_tol=3.0)
    assert estimate.confidence > 0.0
    assert estimate.sample_count == 5


def test_estimate_unavailable_when_all_samples_stale():
    estimator = ProjectileSpeedEstimator(time_source=lambda: 0.0, sample_max_age_s=1.0)
    for i, speed in enumerate([700.0, 700.0, 700.0]):
        estimator.add_sample(speed, now=i * 0.1)
    estimate = estimator.estimate(now=10.0)  # long after every sample aged out
    assert not estimate.available
    assert estimate.confidence == 0.0


def test_non_finite_and_non_positive_samples_are_ignored():
    estimator = ProjectileSpeedEstimator(time_source=lambda: 0.0)
    estimator.add_sample(float("nan"), now=0.0)
    estimator.add_sample(float("inf"), now=0.0)
    estimator.add_sample(-100.0, now=0.0)
    estimator.add_sample(0.0, now=0.0)
    estimate = estimator.estimate(now=0.0)
    assert not estimate.available
    assert estimate.sample_count == 0


def test_high_dispersion_reduces_confidence():
    consistent = ProjectileSpeedEstimator(time_source=lambda: 0.0)
    for i, speed in enumerate([700.0, 701.0, 699.0, 700.0, 700.0]):
        consistent.add_sample(speed, now=i * 0.05)
    consistent_estimate = consistent.estimate(now=0.25)

    scattered = ProjectileSpeedEstimator(time_source=lambda: 0.0)
    for i, speed in enumerate([500.0, 900.0, 400.0, 1000.0, 600.0]):
        scattered.add_sample(speed, now=i * 0.05)
    scattered_estimate = scattered.estimate(now=0.25)

    assert consistent_estimate.confidence > scattered_estimate.confidence


def test_regime_shift_converges_on_new_speed_after_upgrade():
    # Simulate a Bullet Speed upgrade mid-session: speed jumps from ~700 to
    # ~950 px/s and stays there. The estimator must converge on the NEW
    # speed rather than keep reporting a blended average of both regimes.
    estimator = ProjectileSpeedEstimator(time_source=lambda: 0.0, window_size=20)
    t = 0.0
    for speed in [698.0, 702.0, 700.0, 699.0, 701.0, 700.0]:
        estimator.add_sample(speed, now=t)
        t += 0.05
    pre_upgrade = estimator.estimate(now=t)
    assert pre_upgrade.available
    assert math.isclose(pre_upgrade.speed_px_s, 700.0, abs_tol=5.0)

    for speed in [948.0, 952.0, 950.0, 951.0, 949.0]:
        estimator.add_sample(speed, now=t)
        t += 0.05
    post_upgrade = estimator.estimate(now=t)
    assert post_upgrade.available
    assert math.isclose(post_upgrade.speed_px_s, 950.0, abs_tol=10.0), (
        "estimator must converge on the new post-upgrade speed, not a blend of old and new"
    )
