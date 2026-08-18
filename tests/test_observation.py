import numpy as np
import pytest

from deep_eye_oh.observation import Observation, Viewport


def _make_frame(viewport, fill=0):
    return np.full((viewport.height, viewport.width, 3), fill, dtype=np.uint8)


def test_viewport_equality():
    a = Viewport(left=0, top=0, width=4, height=4)
    b = Viewport(left=0, top=0, width=4, height=4)
    c = Viewport(left=1, top=0, width=4, height=4)
    assert a == b
    assert a != c


def test_observation_construction_valid():
    vp = Viewport(left=0, top=0, width=4, height=3)
    frame = _make_frame(vp, fill=7)
    obs = Observation(frame=frame, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)
    assert obs.frame_index == 0
    assert obs.viewport == vp


def test_observation_rejects_wrong_dtype():
    vp = Viewport(left=0, top=0, width=4, height=3)
    frame = np.zeros((3, 4, 3), dtype=np.float32)
    with pytest.raises(ValueError):
        Observation(frame=frame, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)


def test_observation_rejects_wrong_shape():
    vp = Viewport(left=0, top=0, width=4, height=3)
    frame = np.zeros((3, 5, 3), dtype=np.uint8)  # wrong width
    with pytest.raises(ValueError):
        Observation(frame=frame, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)


def test_observation_equality_by_value():
    vp = Viewport(left=0, top=0, width=2, height=2)
    frame_a = _make_frame(vp, fill=9)
    frame_b = _make_frame(vp, fill=9)  # distinct array, same contents
    obs_a = Observation(frame=frame_a, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)
    obs_b = Observation(frame=frame_b, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)
    assert obs_a is not obs_b
    assert obs_a == obs_b


def test_observation_inequality_on_differing_field():
    vp = Viewport(left=0, top=0, width=2, height=2)
    frame = _make_frame(vp, fill=9)
    obs_a = Observation(frame=frame, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)
    obs_b = Observation(frame=frame, timestamp=1.0, monotonic=2.0, frame_index=1, viewport=vp)
    assert obs_a != obs_b


def test_observation_repr_does_not_dump_array():
    vp = Viewport(left=0, top=0, width=2, height=2)
    frame = _make_frame(vp, fill=5)
    obs = Observation(frame=frame, timestamp=1.0, monotonic=2.0, frame_index=0, viewport=vp)
    r = repr(obs)
    assert "shape=(2, 2, 3)" in r
    assert "5, 5, 5" not in r
