"""Exercises window_focus.py with win32gui/win32process monkeypatched --
no real window/cursor state is ever touched."""

import time

from deep_eye_oh import window_focus as wf


def make_target(hwnd=100, pid=200):
    return wf.TargetWindow(hwnd=hwnd, pid=pid, title_at_arm="Notepad")


def test_is_foreground_true_when_all_checks_pass(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "GetForegroundWindow", lambda: target.hwnd)
    monkeypatch.setattr(wf.win32process, "GetWindowThreadProcessId", lambda h: (1, target.pid))
    assert wf.is_foreground(target) is True


def test_is_foreground_false_when_window_gone(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: False)
    assert wf.is_foreground(target) is False


def test_is_foreground_false_when_not_foreground(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "GetForegroundWindow", lambda: target.hwnd + 1)
    assert wf.is_foreground(target) is False


def test_is_foreground_false_when_pid_reused(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "GetForegroundWindow", lambda: target.hwnd)
    monkeypatch.setattr(
        wf.win32process, "GetWindowThreadProcessId", lambda h: (1, target.pid + 1)
    )
    assert wf.is_foreground(target) is False


def test_is_foreground_false_on_exception(monkeypatch):
    target = make_target()

    def _raise(h):
        raise OSError("boom")

    monkeypatch.setattr(wf.win32gui, "IsWindow", _raise)
    assert wf.is_foreground(target) is False


def test_point_is_over_target_match(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "WindowFromPoint", lambda pt: 555)
    monkeypatch.setattr(wf.win32gui, "GetAncestor", lambda h, flag: target.hwnd)
    assert wf.point_is_over_target(target, 10, 10) is True


def test_point_is_over_target_mismatch(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "WindowFromPoint", lambda pt: 555)
    monkeypatch.setattr(wf.win32gui, "GetAncestor", lambda h, flag: target.hwnd + 1)
    assert wf.point_is_over_target(target, 10, 10) is False


def test_point_is_over_target_no_window_under_point(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "WindowFromPoint", lambda pt: 0)
    assert wf.point_is_over_target(target, 10, 10) is False


def test_point_is_over_target_query_raises(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)

    def _raise(pt):
        raise OSError("boom")

    monkeypatch.setattr(wf.win32gui, "WindowFromPoint", _raise)
    assert wf.point_is_over_target(target, 10, 10) is False


def test_cursor_is_over_target_uses_get_cursor_pos(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "GetCursorPos", lambda: (42, 43))
    called = {}

    def fake_point_is_over_target(t, x, y):
        called["args"] = (t, x, y)
        return True

    monkeypatch.setattr(wf, "point_is_over_target", fake_point_is_over_target)
    assert wf.cursor_is_over_target(target) is True
    assert called["args"] == (target, 42, 43)


def test_client_rect_on_screen_success(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    monkeypatch.setattr(wf.win32gui, "GetClientRect", lambda h: (0, 0, 800, 600))
    monkeypatch.setattr(wf.win32gui, "ClientToScreen", lambda h, pt: (100, 50))
    assert wf.client_rect_on_screen(target) == (100, 50, 800, 600)


def test_client_rect_on_screen_none_when_window_gone(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: False)
    assert wf.client_rect_on_screen(target) is None


def test_client_rect_on_screen_none_on_exception(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)

    def _raise(h):
        raise OSError("boom")

    monkeypatch.setattr(wf.win32gui, "GetClientRect", _raise)
    assert wf.client_rect_on_screen(target) is None


def test_client_rect_on_screen_none_when_degenerate(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    # A minimized window can report a zero-size client rect.
    monkeypatch.setattr(wf.win32gui, "GetClientRect", lambda h: (0, 0, 0, 0))
    monkeypatch.setattr(wf.win32gui, "ClientToScreen", lambda h, pt: (0, 0))
    assert wf.client_rect_on_screen(target) is None


def test_target_still_exists(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: True)
    assert wf.target_still_exists(target) is True
    monkeypatch.setattr(wf.win32gui, "IsWindow", lambda h: False)
    assert wf.target_still_exists(target) is False


def _wait_for(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_focus_watcher_trips_on_focus_lost(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf, "is_foreground", lambda t: False)
    trips = []
    watcher = wf.FocusWatcher(
        target, has_held_buttons=lambda: False, on_trip=trips.append, interval_s=0.01
    )
    watcher.start()
    try:
        assert _wait_for(lambda: bool(trips))
    finally:
        watcher.stop(timeout=2)
    assert "focus_lost_async" in trips


def test_focus_watcher_skips_cursor_check_without_held_buttons(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf, "is_foreground", lambda t: True)
    monkeypatch.setattr(wf, "cursor_is_over_target", lambda t: False)
    trips = []
    watcher = wf.FocusWatcher(
        target, has_held_buttons=lambda: False, on_trip=trips.append, interval_s=0.01
    )
    watcher.start()
    time.sleep(0.1)
    watcher.stop(timeout=2)
    assert trips == []


def test_focus_watcher_trips_on_cursor_left_target_while_held(monkeypatch):
    target = make_target()
    monkeypatch.setattr(wf, "is_foreground", lambda t: True)
    monkeypatch.setattr(wf, "cursor_is_over_target", lambda t: False)
    trips = []
    watcher = wf.FocusWatcher(
        target, has_held_buttons=lambda: True, on_trip=trips.append, interval_s=0.01
    )
    watcher.start()
    try:
        assert _wait_for(lambda: bool(trips))
    finally:
        watcher.stop(timeout=2)
    assert "cursor_left_target_while_button_held" in trips


def test_focus_watcher_fails_closed_on_own_exception(monkeypatch):
    target = make_target()

    def _raise(t):
        raise RuntimeError("boom")

    monkeypatch.setattr(wf, "is_foreground", _raise)
    trips = []
    watcher = wf.FocusWatcher(
        target, has_held_buttons=lambda: False, on_trip=trips.append, interval_s=0.01
    )
    watcher.start()
    assert _wait_for(lambda: bool(trips))
    watcher.stop(timeout=2)
    assert "focus_watcher_died" in trips
    assert watcher.is_alive() is False
