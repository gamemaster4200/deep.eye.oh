"""Exercises emergency_stop.py with GetAsyncKeyState monkeypatched -- no
real panic key is ever polled. Console-control-handler registration is
real (start()/stop() are always paired), but the handler function itself
is called directly rather than via a real OS console event."""

import time

import pytest

from deep_eye_oh import emergency_stop as es


def _wait_for(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_high_bit_only_detection(monkeypatch):
    monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: -32768)  # high bit only
    assert es._panic_key_currently_down(0x13) is True

    monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: -32767)  # high + low bit
    assert es._panic_key_currently_down(0x13) is True

    monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: 1)  # low bit only
    assert es._panic_key_currently_down(0x13) is False

    monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: 0)  # neither
    assert es._panic_key_currently_down(0x13) is False


def test_unknown_panic_key_raises():
    with pytest.raises(ValueError):
        es.EmergencyStop(on_trip=lambda r: None, panic_key="nonexistent")


def test_trigger_is_idempotent():
    calls = []
    e = es.EmergencyStop(on_trip=calls.append)
    e.trigger()
    e.trigger()
    assert calls == ["emergency_stop"]
    assert e.is_triggered() is True


def test_emergency_stop_latches_and_does_not_auto_resume(monkeypatch):
    calls = []
    e = es.EmergencyStop(on_trip=calls.append, interval_s=0.01)
    monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: -32768)
    e.start()
    try:
        assert _wait_for(lambda: bool(calls))
        assert calls == ["emergency_stop"]
        assert e.is_triggered() is True

        monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: 0)  # release physical key
        time.sleep(0.1)
        assert calls == ["emergency_stop"]  # no re-fire
        assert e.is_triggered() is True  # latch persists
    finally:
        e.stop()


def test_reset_clears_latch(monkeypatch):
    calls = []
    e = es.EmergencyStop(on_trip=calls.append, interval_s=0.01)
    monkeypatch.setattr(es, "_GetAsyncKeyState", lambda vk: -32768)
    e.start()
    try:
        assert _wait_for(lambda: bool(calls))
        assert e.is_triggered() is True
        e.reset()
        assert e.is_triggered() is False
    finally:
        e.stop()


def test_poll_exception_fails_closed(monkeypatch):
    calls = []

    def _raise(vk):
        raise RuntimeError("boom")

    e = es.EmergencyStop(on_trip=calls.append, interval_s=0.01)
    monkeypatch.setattr(es, "_GetAsyncKeyState", _raise)
    e.start()
    assert _wait_for(lambda: bool(calls))
    e.stop()
    assert "emergency_stop_watchdog_died" in calls
    assert e.is_alive() is False


def test_console_handler_return_values_and_cleanup():
    calls = []
    e = es.EmergencyStop(on_trip=calls.append)
    assert e._handle_console_event(es.CTRL_C_EVENT) is False
    assert e._handle_console_event(es.CTRL_BREAK_EVENT) is False
    assert e._handle_console_event(es.CTRL_CLOSE_EVENT) is True
    assert e._handle_console_event(es.CTRL_LOGOFF_EVENT) is True
    assert e._handle_console_event(es.CTRL_SHUTDOWN_EVENT) is True
    assert calls == ["console_control_event"] * 5
