"""Tests for browser_farming.py: pure helpers (_canvas_origin,
_apply_action, _format_status) against a real (fake-backed) armed
Controller, plus fail-closed single-tick behavior of run_farming_loop
against a fake bridge -- no real window, no real SendInput, no real
WebSocket server."""

import pytest

from deep_eye_oh import browser_farming as bf
from deep_eye_oh import control as ctrl_mod
from deep_eye_oh import win32_input, window_focus
from deep_eye_oh.browser_game_state import BrowserGameState, BrowserShape, CanvasInfo, ScreenTransform
from deep_eye_oh.browser_policy import NOOP, BrowserAction
from deep_eye_oh.window_focus import TargetWindow

TARGET = TargetWindow(hwnd=1, pid=2, title_at_arm="diep.io - Google Chrome")


class FakeWatchdog:
    def __init__(self, *args, on_trip=None, **kwargs):
        self.on_trip = on_trip
        self._alive = True
        self._triggered = False

    def start(self):
        pass

    def stop(self, timeout=None):
        self._alive = False

    def is_alive(self):
        return self._alive

    def is_triggered(self):
        return self._triggered


def _patch_healthy_environment(monkeypatch):
    monkeypatch.setattr(window_focus, "target_still_exists", lambda t: True)
    monkeypatch.setattr(window_focus, "is_foreground", lambda t: True)
    monkeypatch.setattr(window_focus, "cursor_is_over_target", lambda t: True)
    monkeypatch.setattr(window_focus, "point_is_over_target", lambda t, x, y: True)
    monkeypatch.setattr(ctrl_mod, "EmergencyStop", lambda *a, **k: FakeWatchdog(*a, **k))
    monkeypatch.setattr(ctrl_mod, "FocusWatcher", lambda *a, **k: FakeWatchdog(*a, **k))

    sent: list[tuple] = []
    monkeypatch.setattr(win32_input, "send_key_down", lambda name: sent.append(("down", name)))
    monkeypatch.setattr(win32_input, "send_key_up", lambda name: sent.append(("up", name)))
    monkeypatch.setattr(win32_input, "send_mouse_button_down", lambda b: sent.append(("bdown", b)))
    monkeypatch.setattr(win32_input, "send_mouse_button_up", lambda b: sent.append(("bup", b)))
    monkeypatch.setattr(win32_input, "send_mouse_move", lambda x, y: sent.append(("move", x, y)))
    return sent


def _armed_controller(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    c = ctrl_mod.Controller()
    c.arm(TARGET)
    return c, sent


IDENTITY_TRANSFORM = ScreenTransform(scale_x=1.0, scale_y=1.0, offset_x=0.0, offset_y=0.0)


def _shape(shape_class, cx, cy, radius=10.0):
    return BrowserShape(shape_class=shape_class, cx=cx, cy=cy, radius=radius, timestamp_ms=0.0)


def _state(*shapes, canvas=None, received_at=0.0):
    return BrowserGameState(
        shapes=tuple(shapes), canvas=canvas, polled_at_ms=0.0, performance_now_ms=None, received_at=received_at
    )


CANVAS = CanvasInfo(width=1600, height=900, rect_left=0, rect_top=0, rect_width=1600, rect_height=900, device_pixel_ratio=1)


# ---------------------------------------------------------------------------
# _canvas_origin
# ---------------------------------------------------------------------------


def test_canvas_origin_is_canvas_center():
    assert bf._canvas_origin(_state(canvas=CANVAS)) == (800.0, 450.0)


def test_canvas_origin_none_without_canvas():
    assert bf._canvas_origin(_state()) is None


# ---------------------------------------------------------------------------
# _apply_action / _release_all
# ---------------------------------------------------------------------------


def test_apply_action_noop_releases_everything(monkeypatch):
    controller, sent = _armed_controller(monkeypatch)
    controller.press_key("w")
    controller.press_button("left")
    sent.clear()

    held = bf._apply_action(
        controller, NOOP, bf._HeldInputs(move_keys=frozenset({"w"}), shooting=True), IDENTITY_TRANSFORM, TARGET
    )

    assert held == bf._HeldInputs()
    assert ("up", "w") in sent
    assert ("bup", "left") in sent


def test_apply_action_moves_aims_and_shoots(monkeypatch):
    controller, sent = _armed_controller(monkeypatch)
    action = BrowserAction(aim_x=100.0, aim_y=50.0, move_keys=frozenset({"d"}), shoot=True)

    held = bf._apply_action(controller, action, bf._HeldInputs(), IDENTITY_TRANSFORM, TARGET)

    assert held == bf._HeldInputs(move_keys=frozenset({"d"}), shooting=True)
    assert ("move", 100, 50) in sent
    assert ("bdown", "left") in sent
    assert ("down", "d") in sent


def test_apply_action_applies_screen_transform_not_raw_browser_coords(monkeypatch):
    controller, sent = _armed_controller(monkeypatch)
    transform = ScreenTransform(scale_x=0.5, scale_y=0.5, offset_x=10.0, offset_y=20.0)
    action = BrowserAction(aim_x=100.0, aim_y=200.0, move_keys=frozenset(), shoot=False)

    bf._apply_action(controller, action, bf._HeldInputs(), transform, TARGET)

    assert ("move", 60, 120) in sent  # 100*0.5+10=60, 200*0.5+20=120


def test_apply_action_does_not_re_press_already_held_key(monkeypatch):
    controller, sent = _armed_controller(monkeypatch)
    held = bf._HeldInputs(move_keys=frozenset({"w"}), shooting=True)
    controller.press_key("w")
    controller.press_button("left")
    sent.clear()

    action = BrowserAction(aim_x=1.0, aim_y=1.0, move_keys=frozenset({"w"}), shoot=True)
    new_held = bf._apply_action(controller, action, held, IDENTITY_TRANSFORM, TARGET)

    assert ("down", "w") not in sent, "an already-held key must not be re-pressed"
    assert ("bdown", "left") not in sent, "an already-held button must not be re-pressed"
    assert new_held == held


def test_apply_action_switches_movement_keys(monkeypatch):
    controller, sent = _armed_controller(monkeypatch)
    controller.press_key("w")
    held = bf._HeldInputs(move_keys=frozenset({"w"}), shooting=False)
    sent.clear()

    action = BrowserAction(aim_x=1.0, aim_y=1.0, move_keys=frozenset({"d"}), shoot=False)
    new_held = bf._apply_action(controller, action, held, IDENTITY_TRANSFORM, TARGET)

    assert ("up", "w") in sent
    assert ("down", "d") in sent
    assert new_held.move_keys == frozenset({"d"})


def test_apply_action_refuses_when_transformed_point_outside_target_window(monkeypatch):
    # Regression: even a within-canvas shape must not begin/continue
    # aiming/shooting/moving if the TRANSFORMED screen point falls outside
    # the armed window (transform/window-rect mismatch, window moved/
    # resized between telemetry read and input) -- release everything
    # instead of calling move_mouse toward an out-of-window point.
    controller, sent = _armed_controller(monkeypatch)
    monkeypatch.setattr(window_focus, "point_is_over_target", lambda t, x, y: False)
    action = BrowserAction(aim_x=100.0, aim_y=50.0, move_keys=frozenset({"d"}), shoot=True)

    held = bf._apply_action(controller, action, bf._HeldInputs(), IDENTITY_TRANSFORM, TARGET)

    assert held == bf._HeldInputs()
    assert not any(kind in ("move", "bdown", "down") for kind, *_ in sent)


def test_apply_action_stops_continuing_to_shoot_once_point_leaves_target_window(monkeypatch):
    controller, sent = _armed_controller(monkeypatch)
    controller.press_key("d")
    controller.press_button("left")
    held = bf._HeldInputs(move_keys=frozenset({"d"}), shooting=True)
    sent.clear()
    monkeypatch.setattr(window_focus, "point_is_over_target", lambda t, x, y: False)

    action = BrowserAction(aim_x=100.0, aim_y=50.0, move_keys=frozenset({"d"}), shoot=True)
    new_held = bf._apply_action(controller, action, held, IDENTITY_TRANSFORM, TARGET)

    assert new_held == bf._HeldInputs()
    assert ("bup", "left") in sent, "an already-held shot must stop once the target leaves the window"
    assert ("up", "d") in sent


# ---------------------------------------------------------------------------
# run_farming_loop: fail-closed single-tick behavior
# ---------------------------------------------------------------------------


class FakeBridge:
    def __init__(self, state, age):
        self._state = state
        self._age = age
        self.started = False
        self.stopped = False
        self.latest_calls = 0

    def start(self):
        self.started = True

    def stop(self, timeout=None):
        self.stopped = True

    def latest(self):
        self.latest_calls += 1
        return self._state

    def age_s(self, now=None):
        return self._age


def _run_one_tick(monkeypatch, sent, *, state, age, client_rect=(0, 0, 1600, 900)):
    monkeypatch.setattr(window_focus, "arm_foreground_window", lambda: TARGET)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: client_rect)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None: FakeBridge(state, age))
    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0)


def test_run_farming_loop_no_input_when_no_telemetry(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    _run_one_tick(monkeypatch, sent, state=None, age=None)
    assert not any(kind in ("move", "down", "bdown") for kind, *_ in sent)


def test_run_farming_loop_no_input_when_telemetry_stale(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    state = _state(_shape("square", 900.0, 450.0), canvas=CANVAS)
    _run_one_tick(monkeypatch, sent, state=state, age=bf.STALE_AFTER_S + 1.0)
    assert not any(kind in ("move", "down", "bdown") for kind, *_ in sent)


def test_run_farming_loop_no_input_when_canvas_missing(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    state = _state(_shape("square", 900.0, 450.0), canvas=None)
    _run_one_tick(monkeypatch, sent, state=state, age=0.01)
    assert not any(kind in ("move", "down", "bdown") for kind, *_ in sent)


def test_run_farming_loop_no_input_when_client_rect_unavailable(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    state = _state(_shape("square", 900.0, 450.0), canvas=CANVAS)
    _run_one_tick(monkeypatch, sent, state=state, age=0.01, client_rect=None)
    assert not any(kind in ("move", "down", "bdown") for kind, *_ in sent)


def test_run_farming_loop_acts_on_fresh_valid_telemetry(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    state = _state(_shape("square", 900.0, 450.0), canvas=CANVAS)
    _run_one_tick(monkeypatch, sent, state=state, age=0.01)
    assert any(kind == "move" for kind, *_ in sent), "a valid, fresh target must produce a mouse move"
    assert ("bdown", "left") in sent


def test_run_farming_loop_disarms_on_exit(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "arm_foreground_window", lambda: TARGET)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    fake_bridge = FakeBridge(None, None)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None: fake_bridge)

    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0)

    assert fake_bridge.stopped is True


def test_run_farming_loop_no_input_when_only_offscreen_shapes_visible(monkeypatch):
    # End-to-end regression for the live bug: an off-canvas shape (e.g.
    # square @ (376,-195)) must never produce a mouse move/shoot, even
    # though telemetry itself is fresh and otherwise usable.
    sent = _patch_healthy_environment(monkeypatch)
    state = _state(_shape("square", 376.0, -195.0), canvas=CANVAS)
    _run_one_tick(monkeypatch, sent, state=state, age=0.01)
    assert not any(kind in ("move", "down", "bdown") for kind, *_ in sent)


# ---------------------------------------------------------------------------
# run_farming_loop: async Controller trip must stop the loop promptly
# ---------------------------------------------------------------------------


def test_run_farming_loop_stops_promptly_on_async_controller_trip(monkeypatch):
    # Regression: an async trip (FocusWatcher/EmergencyStop, on their own
    # threads) can disarm Controller between ticks. The "no usable
    # telemetry" branch only ever calls the gate-exempt release_all(),
    # which never raises -- without an explicit armed check the loop
    # would keep running (and keep printing "disarmed") indefinitely
    # instead of stopping. Simulate the trip having already happened
    # (as if a background watchdog fired) and confirm the loop exits
    # BEFORE consuming any of its generous max_ticks budget, i.e. before
    # ever calling bridge.latest() again.
    _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "arm_foreground_window", lambda: TARGET)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    fake_bridge = FakeBridge(_state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None: fake_bridge)

    # Trip Controller.arm() into an immediately-disarmed state by making
    # is_foreground fail only at the final arm() health check -- simplest
    # deterministic stand-in for "some background watchdog already fired"
    # without needing arm() to succeed first. Instead, directly simulate
    # a post-arm async trip (what FocusWatcher._check()/EmergencyStop
    # actually do under the hood via _trip_if_epoch), which is the exact
    # scenario this regression targets.
    original_arm = ctrl_mod.Controller.arm

    def arm_then_trip(self, target=None):
        original_arm(self, target)
        self._trip_if_epoch(self._epoch, "cursor_left_target_while_button_held")

    monkeypatch.setattr(ctrl_mod.Controller, "arm", arm_then_trip)

    bf.run_farming_loop(max_ticks=1000, tick_interval_s=0.0)

    assert fake_bridge.latest_calls == 0, "the loop must stop on the armed check before reading telemetry again"


def test_run_farming_loop_prints_trip_reason_when_disarmed(monkeypatch, capsys):
    _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "arm_foreground_window", lambda: TARGET)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    fake_bridge = FakeBridge(None, None)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None: fake_bridge)

    original_arm = ctrl_mod.Controller.arm

    def arm_then_trip(self, target=None):
        original_arm(self, target)
        self._trip_if_epoch(self._epoch, "cursor_left_target_while_button_held")

    monkeypatch.setattr(ctrl_mod.Controller, "arm", arm_then_trip)

    bf.run_farming_loop(max_ticks=1000, tick_interval_s=0.0)

    assert "cursor_left_target_while_button_held" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _controller_status / trip_reason diagnostics
# ---------------------------------------------------------------------------


def test_controller_status_shows_armed(monkeypatch):
    controller, _ = _armed_controller(monkeypatch)
    assert bf._controller_status(controller) == "armed"


def test_controller_status_shows_disarmed_without_reason():
    controller = ctrl_mod.Controller()
    assert bf._controller_status(controller) == "disarmed"


def test_controller_status_shows_disarmed_with_reason(monkeypatch):
    controller, _ = _armed_controller(monkeypatch)
    controller._trip_if_epoch(controller._epoch, "emergency_stop")
    assert bf._controller_status(controller) == "disarmed reason=emergency_stop"


def test_controller_trip_reason_property_is_read_only(monkeypatch):
    controller, _ = _armed_controller(monkeypatch)
    assert controller.trip_reason is None
    controller._trip_if_epoch(controller._epoch, "emergency_stop")
    assert controller.trip_reason == "emergency_stop"
    assert isinstance(type(controller).trip_reason, property)
    assert type(controller).trip_reason.fset is None, "trip_reason must not expose a mutable setter"
