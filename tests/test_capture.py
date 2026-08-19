"""Exercises ScreenCapture with PIL.ImageGrab.grab monkeypatched — no real
display is ever touched, so this stays safe for the standard test suite."""

import numpy as np
import pytest
from PIL import Image

from deep_eye_oh import capture as capture_module
from deep_eye_oh.observation import Viewport


def _fake_grab_factory(width, height, color=(1, 2, 3), calls=None):
    def _fake_grab(bbox=None, all_screens=None):
        if calls is not None:
            calls.append({"bbox": bbox, "all_screens": all_screens})
        return Image.new("RGB", (width, height), color)

    return _fake_grab


def test_grab_one_returns_observation_with_correct_shape(monkeypatch):
    vp = Viewport(left=10, top=20, width=6, height=4)
    monkeypatch.setattr(capture_module.ImageGrab, "grab", _fake_grab_factory(6, 4))
    sc = capture_module.ScreenCapture(vp)
    obs = sc.grab_one(frame_index=3)
    assert obs.frame.shape == (4, 6, 3)
    assert obs.frame.dtype == np.uint8
    assert obs.frame_index == 3
    assert obs.viewport == vp
    assert isinstance(obs.timestamp, float)
    assert isinstance(obs.monotonic, float)


def test_grab_one_requests_all_screens(monkeypatch):
    """Windows Pillow only captures the primary monitor unless all_screens=True
    is passed — without it, any region on a non-primary monitor comes back
    black. This locks that ScreenCapture always requests all screens."""
    vp = Viewport(left=1536, top=0, width=6, height=4)
    calls = []
    monkeypatch.setattr(
        capture_module.ImageGrab, "grab", _fake_grab_factory(6, 4, calls=calls)
    )
    sc = capture_module.ScreenCapture(vp)
    sc.grab_one(frame_index=0)
    assert calls == [{"bbox": (1536, 0, 1542, 4), "all_screens": True}]


def test_grab_one_raises_on_size_mismatch(monkeypatch):
    vp = Viewport(left=0, top=0, width=6, height=4)
    # simulates DPI/monitor-scaling: grab hands back a different pixel size
    monkeypatch.setattr(capture_module.ImageGrab, "grab", _fake_grab_factory(12, 8))
    sc = capture_module.ScreenCapture(vp)
    with pytest.raises(RuntimeError):
        sc.grab_one(frame_index=0)


def test_grab_stream_respects_max_frames(monkeypatch):
    vp = Viewport(left=0, top=0, width=2, height=2)
    monkeypatch.setattr(capture_module.ImageGrab, "grab", _fake_grab_factory(2, 2))
    sc = capture_module.ScreenCapture(vp)
    frames = list(sc.grab_stream(max_frames=5))
    assert len(frames) == 5
    assert [f.frame_index for f in frames] == [0, 1, 2, 3, 4]


def test_grab_stream_respects_duration(monkeypatch):
    vp = Viewport(left=0, top=0, width=2, height=2)
    monkeypatch.setattr(capture_module.ImageGrab, "grab", _fake_grab_factory(2, 2))
    sc = capture_module.ScreenCapture(vp)
    frames = list(sc.grab_stream(duration_s=0.05))
    assert len(frames) >= 1


def test_grab_stream_requires_a_bound(monkeypatch):
    vp = Viewport(left=0, top=0, width=2, height=2)
    monkeypatch.setattr(capture_module.ImageGrab, "grab", _fake_grab_factory(2, 2))
    sc = capture_module.ScreenCapture(vp)
    with pytest.raises(ValueError):
        list(sc.grab_stream())
