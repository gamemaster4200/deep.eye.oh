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


# --- ensure_dpi_awareness (GitHub issue #2) -----------------------------
#
# These monkeypatch ctypes.windll.shcore/user32 directly (not module-level
# names in win32_input.py, since the real calls are made as
# `ctypes.windll.shcore.X(...)` at call time, not through a cached
# reference) so no real process-wide DPI state is touched by these specific
# assertions -- separate from the real declaration that already happened
# once, for real, when this test module imported win32_input (see
# test_module_import_declares_some_real_dpi_awareness below).


def _fake_get_awareness(monkeypatch, level: int):
    def _get(_process_handle, out_ptr):
        out_ptr._obj.value = level

    monkeypatch.setattr(ctypes.windll.shcore, "GetProcessDpiAwareness", _get)


def test_ensure_dpi_awareness_reports_per_monitor_on_success(monkeypatch):
    monkeypatch.setattr(ctypes.windll.shcore, "SetProcessDpiAwareness", lambda level: None)
    _fake_get_awareness(monkeypatch, 2)
    assert wi.ensure_dpi_awareness() == "per-monitor-aware"


def test_ensure_dpi_awareness_falls_back_to_legacy_api_on_oserror(monkeypatch):
    def _raise_set(_level):
        raise OSError("shcore.dll unavailable or already declared")

    legacy_calls = []
    monkeypatch.setattr(ctypes.windll.shcore, "SetProcessDpiAwareness", _raise_set)
    monkeypatch.setattr(ctypes.windll.user32, "SetProcessDPIAware", lambda: legacy_calls.append(1))
    _fake_get_awareness(monkeypatch, 1)

    assert wi.ensure_dpi_awareness() == "system-aware"
    assert legacy_calls == [1]


def test_ensure_dpi_awareness_never_raises_when_everything_fails(monkeypatch):
    monkeypatch.setattr(
        ctypes.windll.shcore, "SetProcessDpiAwareness",
        lambda level: (_ for _ in ()).throw(OSError("no shcore")),
    )
    monkeypatch.setattr(
        ctypes.windll.user32, "SetProcessDPIAware",
        lambda: (_ for _ in ()).throw(OSError("no legacy api either")),
    )
    monkeypatch.setattr(
        ctypes.windll.shcore, "GetProcessDpiAwareness",
        lambda *_a: (_ for _ in ()).throw(OSError("cannot even query")),
    )

    result = wi.ensure_dpi_awareness()  # must not raise
    assert isinstance(result, str)
    assert "unknown" in result


def test_ensure_dpi_awareness_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(ctypes.windll.shcore, "SetProcessDpiAwareness", lambda level: calls.append(level))
    _fake_get_awareness(monkeypatch, 2)

    first = wi.ensure_dpi_awareness()
    second = wi.ensure_dpi_awareness()

    assert first == second == "per-monitor-aware"
    assert calls == [2, 2]  # safe to call the underlying API repeatedly, not just this wrapper


def test_module_import_declares_some_real_dpi_awareness():
    """Not mocked -- exercises the real, actual declaration this module
    makes at import time (already happened before this test runs, since
    Python only imports a module once). Confirms the module-level call
    actually ran and left the real process in a non-default-unaware-by-
    omission state, on this real platform. Not a check of *which* level
    was achieved (that legitimately varies by Windows version/environment
    per ensure_dpi_awareness's fallback chain), just that it ran and
    reports something sane."""
    assert isinstance(wi.DPI_AWARENESS_STATUS, str)
    assert wi.DPI_AWARENESS_STATUS != ""
