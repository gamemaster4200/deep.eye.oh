"""Tests for target_tracking.py: minimal single-target motion tracking."""

import math

from deep_eye_oh.target_tracking import TargetCandidate, TargetTracker


def _c(cx, cy, t, radius=15.0):
    return TargetCandidate(cx=cx, cy=cy, radius=radius, timestamp_ms=t)


def test_no_observation_from_a_single_sighting():
    tracker = TargetTracker()
    result = tracker.update([_c(100.0, 100.0, 0.0)], now_ms=0.0)
    assert result is None
    assert tracker.has_target


def test_missing_observation_returns_none_and_no_target_initially():
    tracker = TargetTracker()
    result = tracker.update([], now_ms=0.0)
    assert result is None
    assert not tracker.has_target


def test_two_consistent_sightings_produce_velocity():
    tracker = TargetTracker()
    tracker.update([_c(100.0, 100.0, 0.0)], now_ms=0.0)
    result = tracker.update([_c(150.0, 100.0, 50.0)], now_ms=50.0)  # +50px over 50ms
    assert result is not None
    assert math.isclose(result.vx, 1000.0, rel_tol=1e-6)
    assert math.isclose(result.vy, 0.0, abs_tol=1e-6)
    assert result.cx == 150.0
    assert result.cy == 100.0


def test_velocity_uses_real_timestamps_not_fixed_frequency():
    tracker = TargetTracker()
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    # A much longer real gap than a typical ~50ms tick -- velocity must
    # reflect the ACTUAL elapsed time (200ms), not an assumed fixed one.
    result = tracker.update([_c(40.0, 0.0, 200.0)], now_ms=200.0)
    assert result is not None
    assert math.isclose(result.vx, 200.0, rel_tol=1e-6)  # 40px / 0.2s


def test_missing_observation_mid_track_decays_confidence_without_crashing():
    tracker = TargetTracker()
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    tracker.update([_c(50.0, 0.0, 50.0)], now_ms=50.0)
    result = tracker.update([], now_ms=100.0)
    assert result is None  # a missed observation is never a confident output


def test_track_dropped_after_confidence_exhausted_by_repeated_misses():
    tracker = TargetTracker()
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    for tick in range(1, 6):
        tracker.update([], now_ms=float(tick) * 50.0)
    assert not tracker.has_target


def test_ambiguous_association_does_not_guess():
    tracker = TargetTracker()
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    # Two equally-plausible candidates -- must not arbitrarily pick one.
    result = tracker.update([_c(30.0, 5.0, 50.0), _c(30.0, -5.0, 50.0)], now_ms=50.0)
    assert result is None


def test_implausible_jump_is_rejected_as_a_candidate():
    tracker = TargetTracker(max_jump_px=100.0)
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    # 5000px in 50ms is not a plausible continuation of the same target.
    result = tracker.update([_c(5000.0, 0.0, 50.0)], now_ms=50.0)
    assert result is None


def test_stale_out_of_order_sample_is_ignored_not_divided_by_zero():
    tracker = TargetTracker()
    tracker.update([_c(0.0, 0.0, 100.0)], now_ms=100.0)
    # A candidate with a timestamp at or before the last known one (e.g. a
    # duplicate/out-of-order delivery) must not corrupt velocity via
    # division by zero or a negative dt.
    result = tracker.update([_c(10.0, 0.0, 100.0)], now_ms=100.0)
    assert result is None


def test_low_confidence_observation_not_surfaced_until_threshold_crossed():
    tracker = TargetTracker(min_confidence=0.9)
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    result = tracker.update([_c(10.0, 0.0, 50.0)], now_ms=50.0)
    assert result is None, "confidence after only one confirmed step must not yet exceed a 0.9 bar"


def test_a_second_unrelated_candidate_does_not_steal_the_track():
    tracker = TargetTracker()
    tracker.update([_c(0.0, 0.0, 0.0)], now_ms=0.0)
    result = tracker.update(
        [_c(20.0, 0.0, 50.0), _c(900.0, 900.0, 50.0)],  # real continuation + unrelated far-away circle
        now_ms=50.0,
    )
    assert result is not None
    assert result.cx == 20.0
