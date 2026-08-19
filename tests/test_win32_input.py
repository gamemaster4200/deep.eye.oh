"""Exercises win32_input.py entirely via a monkeypatched SendInput call
point -- no real key/mouse events are ever injected."""

import ctypes

import pytest

from deep_eye_oh import win32_input as wi


def test_input_struct_size_matches_platform():
    expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
    assert ctypes.sizeof(wi.INPUT) == expected


def _capture_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(wi, "_send_raw_input", lambda inputs: sent.append(inputs))
    return sent


def test_send_key_down_uses_scancode_flag(monkeypatch):
    sent = _capture_sent(monkeypatch)
    wi.send_key_down("w")
    (inputs,) = sent
    (inp,) = inputs
    assert inp.type == wi.INPUT_KEYBOARD
    assert inp.ki.wScan == wi._SCAN_CODES["w"]
    assert inp.ki.dwFlags & wi.KEYEVENTF_SCANCODE
    assert not (inp.ki.dwFlags & wi.KEYEVENTF_KEYUP)
    assert inp.ki.wVk == 0


def test_send_key_up_sets_keyup_flag(monkeypatch):
    sent = _capture_sent(monkeypatch)
    wi.send_key_up("a")
    (inputs,) = sent
    (inp,) = inputs
    assert inp.ki.wScan == wi._SCAN_CODES["a"]
    assert inp.ki.dwFlags & wi.KEYEVENTF_KEYUP
    assert inp.ki.dwFlags & wi.KEYEVENTF_SCANCODE


def test_send_key_unknown_name_raises(monkeypatch):
    _capture_sent(monkeypatch)
    with pytest.raises(ValueError):
        wi.send_key_down("nonexistent")


@pytest.mark.parametrize(
    ("x", "y", "vx", "vy", "vw", "vh", "expected"),
    [
        (0, 0, 0, 0, 1920, 1080, (0, 0)),
        (1919, 1079, 0, 0, 1920, 1080, (65535, 65535)),
        (-1536, 0, -1536, 0, 1536, 864, (0, 0)),
        (-1, 863, -1536, 0, 1536, 864, (65535, 65535)),
    ],
)
def test_normalize_to_virtual_desktop_endpoints(x, y, vx, vy, vw, vh, expected):
    assert wi.normalize_to_virtual_desktop(x, y, vx, vy, vw, vh) == expected


def test_send_mouse_move_normalizes_against_virtual_desktop(monkeypatch):
    sent = _capture_sent(monkeypatch)
    monkeypatch.setattr(wi, "_virtual_desktop_metrics", lambda: (-1536, 0, 3072, 960))
    wi.send_mouse_move(-1536, 0)
    (inputs,) = sent
    (inp,) = inputs
    assert inp.type == wi.INPUT_MOUSE
    assert inp.mi.dx == 0
    assert inp.mi.dy == 0
    assert inp.mi.dwFlags & wi.MOUSEEVENTF_ABSOLUTE
    assert inp.mi.dwFlags & wi.MOUSEEVENTF_VIRTUALDESK
    assert inp.mi.dwFlags & wi.MOUSEEVENTF_MOVE


def test_send_mouse_button_down_up_flags(monkeypatch):
    sent = _capture_sent(monkeypatch)
    wi.send_mouse_button_down("left")
    wi.send_mouse_button_up("left")
    down = sent[0][0]
    up = sent[1][0]
    assert down.mi.dwFlags == wi.MOUSEEVENTF_LEFTDOWN
    assert up.mi.dwFlags == wi.MOUSEEVENTF_LEFTUP


def test_send_mouse_button_unknown_raises(monkeypatch):
    _capture_sent(monkeypatch)
    with pytest.raises(ValueError):
        wi.send_mouse_button_down("nonexistent")


def test_send_raw_input_raises_on_partial_send(monkeypatch):
    monkeypatch.setattr(wi, "_SendInput", lambda count, arr, size: count - 1)
    with pytest.raises(OSError):
        wi._send_raw_input([wi._key_input(wi._SCAN_CODES["w"], key_up=False)])
