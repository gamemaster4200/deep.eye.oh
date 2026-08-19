"""Exercises control.py's Controller entirely against fakes -- no real
SendInput, no real window/focus queries, no real watchdog threads. Fakes
stand in for EmergencyStop/FocusWatcher so Controller's own lock
discipline, epoch/generation logic, and gate ordering can be driven
deterministically."""

import threading
import time

import pytest

from deep_eye_oh import control as ctrl_mod
from deep_eye_oh import win32_input, window_focus
from deep_eye_oh.window_focus import TargetWindow

TARGET = TargetWindow(hwnd=1, pid=2, title_at_arm="Notepad")


class FakeWatchdog:
    """Stands in for both EmergencyStop and FocusWatcher."""

    def __init__(self, *args, on_trip=None, **kwargs):
        self.on_trip = on_trip
        self.started = False
        self.stopped = False
        self._alive = True
        self._triggered = False

    def start(self) -> None:
        self.started = True

    def stop(self, timeout=None) -> None:
        self.stopped = True
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def is_triggered(self) -> bool:
        return self._triggered

    def fire(self, reason: str) -> None:
        assert self.on_trip is not None
        self.on_trip(reason)


def _patch_healthy_environment(monkeypatch):
    """Everything reports healthy/safe by default; individual tests
    override specific pieces. Returns (watchdogs, sent) -- watchdogs is
    populated with 'estop'/'focus' keys as Controller.arm() constructs
    them; sent records every injected input call."""
    monkeypatch.setattr(window_focus, "target_still_exists", lambda t: True)
    monkeypatch.setattr(window_focus, "is_foreground", lambda t: True)
    monkeypatch.setattr(window_focus, "cursor_is_over_target", lambda t: True)
    monkeypatch.setattr(window_focus, "point_is_over_target", lambda t, x, y: True)

    watchdogs: dict[str, FakeWatchdog] = {}

    def make_estop(*args, **kwargs):
        w = FakeWatchdog(*args, **kwargs)
        watchdogs["estop"] = w
        return w

    def make_focus(*args, **kwargs):
        w = FakeWatchdog(*args, **kwargs)
        watchdogs["focus"] = w
        return w

    monkeypatch.setattr(ctrl_mod, "EmergencyStop", make_estop)
    monkeypatch.setattr(ctrl_mod, "FocusWatcher", make_focus)

    sent: list[tuple] = []
    monkeypatch.setattr(win32_input, "send_key_down", lambda name: sent.append(("down", name)))
    monkeypatch.setattr(win32_input, "send_key_up", lambda name: sent.append(("up", name)))
    monkeypatch.setattr(
        win32_input, "send_mouse_button_down", lambda b: sent.append(("bdown", b))
    )
    monkeypatch.setattr(win32_input, "send_mouse_button_up", lambda b: sent.append(("bup", b)))
    monkeypatch.setattr(win32_input, "send_mouse_move", lambda x, y: sent.append(("move", x, y)))

    return watchdogs, sent


def _armed_controller(monkeypatch):
    watchdogs, sent = _patch_healthy_environment(monkeypatch)
    c = ctrl_mod.Controller()
    c.arm(TARGET)
    return c, watchdogs, sent


def _wait_for(predicate, timeout=3.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---- gate ordering / individual trip reasons ---------------------------


def test_press_key_refused_when_not_armed(monkeypatch):
    _patch_healthy_environment(monkeypatch)
    c = ctrl_mod.Controller()
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_key("w")


def test_press_key_refused_when_emergency_stop_triggered(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    watchdogs["estop"]._triggered = True
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_key("w")
    assert c.armed is False


def test_press_key_refused_when_watchdog_dead(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    watchdogs["estop"]._alive = False
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_key("w")
    assert c.armed is False


def test_press_key_refused_when_focus_lost_sync(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    monkeypatch.setattr(window_focus, "is_foreground", lambda t: False)
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_key("w")
    assert c.armed is False


def test_press_button_refused_when_cursor_not_over_target(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    monkeypatch.setattr(window_focus, "cursor_is_over_target", lambda t: False)
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_button("left")
    assert c.armed is False


def test_gate_catches_triggered_estop_even_before_callback_runs(monkeypatch):
    """Round-6: a synchronous is_triggered() check closes the race where
    the watchdog latched internally but hasn't yet acquired the lock to
    report it via the callback."""
    c, watchdogs, sent = _armed_controller(monkeypatch)
    watchdogs["estop"]._triggered = True  # latched, callback never invoked
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_key("w")
    assert c.armed is False


def test_armed_never_true_immediately_after_any_trip(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    watchdogs["estop"].fire("emergency_stop")
    assert c.armed is False


# ---- mouse move / held-button destination safety -----------------------


def test_move_mouse_unrestricted_without_held_button(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    monkeypatch.setattr(window_focus, "point_is_over_target", lambda t, x, y: False)
    c.move_mouse(10, 10)
    assert ("move", 10, 10) in sent


def test_move_mouse_refused_when_button_held_and_destination_mismatched(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    c.press_button("left")
    monkeypatch.setattr(window_focus, "point_is_over_target", lambda t, x, y: False)
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.move_mouse(999, 999)
    assert c.armed is False


def test_move_mouse_allowed_when_button_held_and_destination_matches(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    c.press_button("left")
    c.move_mouse(5, 5)
    assert ("move", 5, 5) in sent


def test_has_held_buttons_reflects_state(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    assert c._has_held_buttons() is False
    c.press_button("left")
    assert c._has_held_buttons() is True
    c.release_button("left")
    assert c._has_held_buttons() is False


# ---- release_all retry-safety -------------------------------------------


def test_release_all_retries_failed_release(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    c.press_key("w")

    state = {"failed_once": False}

    def flaky_send_key_up(name):
        if not state["failed_once"]:
            state["failed_once"] = True
            raise OSError("simulated failure")
        sent.append(("up", name))

    monkeypatch.setattr(win32_input, "send_key_up", flaky_send_key_up)

    c.release_all()
    assert "w" in c._held_keys  # failed release stays tracked

    c.release_all()  # retry succeeds
    assert "w" not in c._held_keys
    assert ("up", "w") in sent


# ---- lock discipline -----------------------------------------------------


def test_tap_key_does_not_hold_lock_during_dwell(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)

    t = threading.Thread(target=lambda: c.tap_key("w", dwell_s=0.3))
    t.start()
    time.sleep(0.05)  # tap has pressed and is now sleeping, lock should be free

    start = time.time()
    c.press_key("a")
    elapsed = time.time() - start
    t.join(timeout=3)
    assert elapsed < 0.2, f"press_key blocked {elapsed}s -- lock held during dwell?"


def test_race_press_vs_trip_never_leaves_key_stuck(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    estop = watchdogs["estop"]

    send_down_called = threading.Event()
    allow_send_down_to_finish = threading.Event()

    def blocking_send_key_down(name):
        send_down_called.set()
        allow_send_down_to_finish.wait(timeout=3)
        sent.append(("down", name))

    monkeypatch.setattr(win32_input, "send_key_down", blocking_send_key_down)

    def do_press():
        try:
            c.press_key("w")
        except ctrl_mod.ControlNotSafeError:
            pass

    press_thread = threading.Thread(target=do_press)
    press_thread.start()
    assert send_down_called.wait(timeout=3)

    trip_done = threading.Event()

    def do_trip():
        estop.fire("emergency_stop")
        trip_done.set()

    trip_thread = threading.Thread(target=do_trip)
    trip_thread.start()
    time.sleep(0.1)  # give the trip thread a chance to start blocking on the lock

    allow_send_down_to_finish.set()  # let press_key finish recording "w" as held

    press_thread.join(timeout=3)
    trip_thread.join(timeout=3)

    assert trip_done.is_set()
    assert "w" not in c._held_keys


def test_shutdown_never_holds_lock_during_watchdog_stop(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    estop = watchdogs["estop"]

    def stop_that_tries_to_trip(timeout=None):
        # If shutdown() were still holding Controller._lock when it calls
        # this, firing a trip (which needs the same lock) would deadlock.
        estop.fire("emergency_stop")

    estop.stop = stop_that_tries_to_trip

    done = threading.Event()

    def do_shutdown():
        c.shutdown()
        done.set()

    t = threading.Thread(target=do_shutdown, daemon=True)
    t.start()
    completed = done.wait(timeout=3)
    assert completed, "shutdown() deadlocked -- lock was held during watcher stop()"


# ---- arm() transactionality / epoch model ------------------------------


def test_arm_fails_if_tripped_during_startup(monkeypatch):
    watchdogs, sent = _patch_healthy_environment(monkeypatch)

    def make_focus_that_trips(*args, **kwargs):
        w = FakeWatchdog(*args, **kwargs)
        w.fire("focus_lost_async")
        return w

    monkeypatch.setattr(ctrl_mod, "FocusWatcher", make_focus_that_trips)

    c = ctrl_mod.Controller()
    with pytest.raises(ctrl_mod.ControlArmFailedError):
        c.arm(TARGET)
    assert c.armed is False


def test_arm_fails_if_watchdog_not_alive(monkeypatch):
    watchdogs, sent = _patch_healthy_environment(monkeypatch)

    def make_dead_estop(*args, **kwargs):
        w = FakeWatchdog(*args, **kwargs)
        w._alive = False
        return w

    monkeypatch.setattr(ctrl_mod, "EmergencyStop", make_dead_estop)

    c = ctrl_mod.Controller()
    with pytest.raises(ctrl_mod.ControlArmFailedError):
        c.arm(TARGET)
    assert c.armed is False


def test_stale_epoch_trip_does_not_affect_newer_session(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    current_epoch = c._epoch

    c._trip_if_epoch(current_epoch - 1, "stale_reason")
    assert c.armed is True

    c._trip_if_epoch(current_epoch, "current_reason")
    assert c.armed is False


def test_concurrent_shutdown_invalidates_in_progress_arm(monkeypatch):
    watchdogs, sent = _patch_healthy_environment(monkeypatch)
    c = ctrl_mod.Controller()

    def make_focus_that_triggers_concurrent_shutdown(*args, **kwargs):
        w = FakeWatchdog(*args, **kwargs)
        c.shutdown()  # simulates a concurrent disarm/shutdown racing this arm()
        return w

    monkeypatch.setattr(ctrl_mod, "FocusWatcher", make_focus_that_triggers_concurrent_shutdown)

    with pytest.raises(ctrl_mod.ControlArmFailedError):
        c.arm(TARGET)
    assert c.armed is False


def test_regain_focus_after_trip_does_not_resume(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)
    c._trip_if_epoch(c._epoch, "focus_lost_async")
    assert c.armed is False

    monkeypatch.setattr(window_focus, "is_foreground", lambda t: True)  # focus "restored"
    with pytest.raises(ctrl_mod.ControlNotSafeError):
        c.press_key("w")
    assert c.armed is False


# ---- generation-aware delayed releases (round-8) ------------------------


def test_delayed_tap_release_is_epoch_checked(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)

    t = threading.Thread(target=lambda: c.tap_key("w", dwell_s=0.3))
    t.start()
    time.sleep(0.05)  # press has happened; tap is now sleeping outside the lock

    c.shutdown()
    _patch_healthy_environment(monkeypatch)
    c.arm(TARGET)
    c.press_key("w")  # a new, unrelated session also legitimately holds "w"

    t.join(timeout=3)  # old tap's delayed release fires during/after this

    assert "w" in c._held_keys, "stale delayed release incorrectly released the new session's key"
    assert c.armed is True


def test_delayed_click_release_is_epoch_checked(monkeypatch):
    c, watchdogs, sent = _armed_controller(monkeypatch)

    t = threading.Thread(target=lambda: c.click("left", dwell_s=0.3))
    t.start()
    time.sleep(0.05)

    c.shutdown()
    _patch_healthy_environment(monkeypatch)
    c.arm(TARGET)
    c.press_button("left")

    t.join(timeout=3)

    assert "left" in c._held_buttons
    assert c.armed is True
