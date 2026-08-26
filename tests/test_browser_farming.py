"""Tests for browser_farming.py: pure helpers (_canvas_origin,
_apply_action, _format_status) against a real (fake-backed) armed
Controller, plus fail-closed single-tick behavior, startup-sequencing
behavior (browser-lifecycle-v0: CAPTCHA/loading/lobby/entering-game
waiting, arm-only-after-PLAYING-plus-fresh-Oracle), and the per-tick
runtime gameplay gate (DEAD/stale-lifecycle/UNKNOWN suppression, no
acting on a pre-respawn Oracle snapshot, latching safety) against fake
browser_runtime/window_focus/browser_lifecycle/bridge dependencies -- no
real Chrome process, no real window, no real SendInput, no real
WebSocket server."""

import time
from pathlib import Path

import pytest

from deep_eye_oh import browser_farming as bf
from deep_eye_oh import browser_lifecycle, browser_runtime, paths
from deep_eye_oh import control as ctrl_mod
from deep_eye_oh import win32_input, window_focus
from deep_eye_oh.browser_game_state import BrowserCircle, BrowserGameState, BrowserShape, CanvasInfo, ScreenTransform
from deep_eye_oh.browser_lifecycle import BrowserFarmConfig, BrowserLifecycleSnapshot, BrowserLifecycleState
from deep_eye_oh.browser_policy import NOOP, BrowserAction
from deep_eye_oh.intercept import solve_intercept
from deep_eye_oh.window_focus import TargetWindow

TARGET = TargetWindow(hwnd=1, pid=2, title_at_arm="diep.io - Google Chrome")
FAKE_CHROME_EXE = Path("C:/fake/chrome-win64/chrome.exe")
FAKE_EXTENSION_DIR = Path("C:/fake/extension")


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


def _circle(cx, cy, timestamp_ms=0.0, radius=3.0, color=None):
    return BrowserCircle(cx=cx, cy=cy, radius=radius, color=color, timestamp_ms=timestamp_ms)


def _state(*shapes, canvas=None, circles=(), received_at=0.0):
    return BrowserGameState(
        shapes=tuple(shapes), circles=tuple(circles), canvas=canvas,
        polled_at_ms=0.0, performance_now_ms=None, received_at=received_at,
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
# FakeBridge: a single unified fake covering both the Oracle and lifecycle
# telemetry slots, used by every startup/runtime-gate test below.
# ---------------------------------------------------------------------------


def _sequence(value):
    """A bare (non-list/tuple) value becomes a single-item list (read
    forever); a list/tuple is used as-is and held at its last entry once
    exhausted."""
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _lifecycle_snapshot(state):
    if state is None:
        return None
    return BrowserLifecycleSnapshot(state=state, reason="test", selected_mode="ffa", received_at=0.0)


class FakeBridge:
    """Oracle and lifecycle telemetry are independent, each-call-advancing
    (held at the last entry once exhausted) sequences -- e.g.
    `lifecycle=[PLAYING, DEAD]` reports PLAYING on the first call
    (startup) and DEAD on every call after (the main tick loop).

    Oracle `received_at` defaults to being dynamically restamped to
    `time.monotonic()` on every latest() call (auto-fresh) -- a real
    bridge's telemetry is always "just received" in a fast test loop,
    and this is what lets every EXISTING (pre-browser-lifecycle-v0) test
    fixture satisfy the new post-PLAYING-transition freshness gate
    without computing a live timestamp itself. Pass an explicit
    `oracle_received_at` sequence for tests that need precise control
    over that (e.g. "a pre-respawn snapshot must never cause action").
    """

    def __init__(
        self,
        state=None,
        age=None,
        *,
        connected=True,
        lifecycle=BrowserLifecycleState.PLAYING,
        lifecycle_age=0.0,
        oracle_received_at=None,
        freeze_oracle_received_at=False,
    ):
        self._oracle_states = _sequence(state)
        self._oracle_ages = _sequence(age)
        self._oracle_received_ats = _sequence(oracle_received_at) if oracle_received_at is not None else None
        self._freeze_oracle_received_at = freeze_oracle_received_at
        self._frozen_received_at = None
        self._oracle_index = -1

        self._lifecycle_states = _sequence(lifecycle)
        self._lifecycle_ages = _sequence(lifecycle_age)
        self._lifecycle_index = -1

        self._connected = connected
        self.started = False
        self.stopped = False
        self.latest_calls = 0

    def start(self):
        self.started = True

    def stop(self, timeout=None):
        self.stopped = True

    def has_connected(self):
        return self._connected

    def latest(self):
        self.latest_calls += 1
        if self._oracle_index + 1 < len(self._oracle_states):
            self._oracle_index += 1
        state = self._oracle_states[self._oracle_index]
        if state is None:
            return None
        if self._oracle_received_ats is not None:
            idx = min(self._oracle_index, len(self._oracle_received_ats) - 1)
            received_at = self._oracle_received_ats[idx]
        elif self._freeze_oracle_received_at:
            if self._frozen_received_at is None:
                self._frozen_received_at = time.monotonic()
            received_at = self._frozen_received_at
        else:
            received_at = time.monotonic()
        return BrowserGameState(
            shapes=state.shapes, circles=state.circles, canvas=state.canvas,
            polled_at_ms=state.polled_at_ms, performance_now_ms=state.performance_now_ms,
            received_at=received_at,
        )

    def age_s(self, now=None):
        idx = max(self._oracle_index, 0)
        idx = min(idx, len(self._oracle_ages) - 1)
        return self._oracle_ages[idx]

    def latest_lifecycle(self):
        if self._lifecycle_index + 1 < len(self._lifecycle_states):
            self._lifecycle_index += 1
        return _lifecycle_snapshot(self._lifecycle_states[self._lifecycle_index])

    def lifecycle_age_s(self, now=None):
        idx = max(self._lifecycle_index, 0)
        idx = min(idx, len(self._lifecycle_ages) - 1)
        return self._lifecycle_ages[idx]


class FakeChromeProcess:
    """Stand-in for subprocess.Popen -- run_farming_loop only ever reads
    .pid and hands the whole object to browser_runtime.terminate_chrome
    (itself monkeypatched below), so no real .poll()/.wait() semantics are
    needed here."""

    def __init__(self, pid=4321):
        self.pid = pid


def _patch_startup(
    monkeypatch,
    *,
    connected=True,
    arm_target=TARGET,
    arm_error: Exception | None = None,
    config: BrowserFarmConfig | None = None,
):
    """Patches every dependency run_farming_loop's startup sequence talks
    to *before* Controller.arm() -- Chrome resolution/launch/teardown, the
    bundled extension path, window-arming, and the stored browser-farm
    config (never touches the real filesystem/LOCALAPPDATA) -- with fast,
    deterministic fakes. Returns (terminate_calls,) so callers can assert
    on teardown calls."""
    monkeypatch.setattr(browser_runtime, "find_or_download_chrome", lambda: FAKE_CHROME_EXE)
    monkeypatch.setattr(paths, "resolve_extension_dir", lambda: FAKE_EXTENSION_DIR)
    monkeypatch.setattr(browser_runtime, "launch_chrome", lambda *a, **k: FakeChromeProcess())
    monkeypatch.setattr(browser_lifecycle, "load_config", lambda: config or BrowserFarmConfig())

    terminate_calls: list[FakeChromeProcess] = []
    monkeypatch.setattr(browser_runtime, "terminate_chrome", lambda proc, **k: terminate_calls.append(proc))

    def _arm_process_window(pid, timeout_s=None, **kwargs):
        if arm_error is not None:
            raise arm_error
        return arm_target

    monkeypatch.setattr(window_focus, "arm_process_window", _arm_process_window)
    return terminate_calls


def _run_one_tick(monkeypatch, sent, *, state, age, client_rect=(0, 0, 1600, 900), lifecycle=BrowserLifecycleState.PLAYING):
    """Runs the loop through a successful startup (immediately PLAYING,
    fresh Oracle) straight into exactly one tick."""
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: client_rect)
    _patch_startup(monkeypatch)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: FakeBridge(state, age, lifecycle=lifecycle))
    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0)


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


def test_run_farming_loop_never_aims_at_an_offscreen_shape(monkeypatch):
    # End-to-end regression for the live bug: an off-canvas shape (e.g.
    # square @ (376,-195)) must never itself become the aim target, even
    # though telemetry itself is fresh and otherwise usable. (Tick 1 does
    # still fire ONE real click at canvas center regardless of any visible
    # target -- see entered_playing_this_tick's own comment in
    # browser_farming.py -- but that click must never land on/aim at the
    # offscreen shape's own would-be screen point.)
    sent = _patch_healthy_environment(monkeypatch)
    state = _state(_shape("square", 376.0, -195.0), canvas=CANVAS)
    _run_one_tick(monkeypatch, sent, state=state, age=0.01)

    offscreen_screen_point = IDENTITY_TRANSFORM.apply(376.0, -195.0)
    assert ("move", *offscreen_screen_point) not in sent, "the offscreen shape must never itself become the aim target"


def test_run_farming_loop_cleans_up_on_normal_exit(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    terminate_calls = _patch_startup(monkeypatch)
    state = _state(_shape("square", 900.0, 450.0), canvas=CANVAS)
    fake_bridge = FakeBridge(state, 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0)

    assert fake_bridge.stopped is True
    assert len(terminate_calls) == 1, "the spawned Chrome process must be torn down exactly once"


# ---------------------------------------------------------------------------
# run_farming_loop: Controller must stay disarmed until the whole
# browser/extension/bridge/LIFECYCLE readiness path is proven working
# ---------------------------------------------------------------------------


def test_run_farming_loop_raises_and_never_arms_when_extension_never_connects(monkeypatch):
    _patch_healthy_environment(monkeypatch)
    terminate_calls = _patch_startup(monkeypatch, connected=False)
    fake_bridge = FakeBridge(connected=False)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    with pytest.raises(bf.BrowserFarmStartupError, match="never connected"):
        bf.run_farming_loop(ready_connect_timeout_s=0.05, tick_interval_s=0.0)

    assert fake_bridge.stopped is True, "the bridge must still be stopped on a pre-arm startup failure"
    assert len(terminate_calls) == 1, "the spawned Chrome process must still be torn down on a pre-arm startup failure"


def test_run_farming_loop_never_arms_when_window_arming_fails(monkeypatch):
    _patch_healthy_environment(monkeypatch)
    terminate_calls = _patch_startup(monkeypatch, arm_error=RuntimeError("no window appeared"))
    fake_bridge = FakeBridge(_state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    with pytest.raises(RuntimeError, match="no window appeared"):
        bf.run_farming_loop(tick_interval_s=0.0)

    assert fake_bridge.stopped is True
    assert len(terminate_calls) == 1


def test_run_farming_loop_arms_only_after_readiness_succeeds(monkeypatch):
    # Regression for the arm-after-readiness correction: Controller.arm()
    # must not be called (and therefore no input can ever be sent) until
    # both readiness stages have already succeeded.
    sent = _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)

    arm_calls: list[TargetWindow] = []
    original_arm = ctrl_mod.Controller.arm

    def recording_arm(self, target=None):
        arm_calls.append(target)
        original_arm(self, target)

    monkeypatch.setattr(ctrl_mod.Controller, "arm", recording_arm)

    fake_bridge = FakeBridge(_state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0)

    assert arm_calls == [TARGET], "Controller.arm() must be called exactly once, after readiness succeeded"


# ---------------------------------------------------------------------------
# browser-lifecycle-v0: startup waits out CAPTCHA/LOADING/LOBBY/
# ENTERING_GAME before ever arming, and CAPTCHA never counts against the
# non-CAPTCHA stuck-diagnostic timeout.
# ---------------------------------------------------------------------------


def test_startup_does_not_arm_during_captcha(monkeypatch, capsys):
    _patch_healthy_environment(monkeypatch)
    _patch_startup(monkeypatch)

    arm_calls: list[TargetWindow] = []
    original_arm = ctrl_mod.Controller.arm

    def recording_arm(self, target=None):
        arm_calls.append(target)
        original_arm(self, target)

    monkeypatch.setattr(ctrl_mod.Controller, "arm", recording_arm)

    # Stays CAPTCHA_REQUIRED for several polls (well past a would-be-tiny
    # timeout -- see test below), then resolves.
    lifecycle_sequence = [BrowserLifecycleState.CAPTCHA_REQUIRED] * 4 + [BrowserLifecycleState.PLAYING]
    fake_bridge = FakeBridge(
        _state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01, lifecycle=lifecycle_sequence,
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0, lifecycle_ready_timeout_s=0.05)

    assert arm_calls == [TARGET], "Controller must arm only once CAPTCHA is actually resolved"
    out = capsys.readouterr().out
    assert "CAPTCHA required." in out
    assert "Complete it manually in the managed browser." in out
    assert "Controller is not armed." in out
    assert "browser lifecycle: CAPTCHA_REQUIRED" in out
    assert "browser lifecycle: PLAYING" in out


def test_captcha_wait_is_not_killed_by_a_short_non_captcha_timeout(monkeypatch):
    # The whole point of pausing the budget during CAPTCHA_REQUIRED: even
    # a tiny non-CAPTCHA timeout must not fire while genuinely stuck on
    # CAPTCHA, only once genuinely stuck on something else.
    _patch_healthy_environment(monkeypatch)
    _patch_startup(monkeypatch)
    lifecycle_sequence = [BrowserLifecycleState.CAPTCHA_REQUIRED] * 6 + [BrowserLifecycleState.PLAYING]
    fake_bridge = FakeBridge(
        _state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01, lifecycle=lifecycle_sequence,
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    # 6 CAPTCHA polls * the internal 0.1s poll interval > this 0.02s
    # budget -- if CAPTCHA did not pause the deadline, this would raise.
    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0, lifecycle_ready_timeout_s=0.02)  # must not raise


def test_startup_does_not_arm_during_loading_or_lobby(monkeypatch):
    _patch_healthy_environment(monkeypatch)
    _patch_startup(monkeypatch)

    arm_calls: list[TargetWindow] = []
    original_arm = ctrl_mod.Controller.arm

    def recording_arm(self, target=None):
        arm_calls.append(target)
        original_arm(self, target)

    monkeypatch.setattr(ctrl_mod.Controller, "arm", recording_arm)

    lifecycle_sequence = [
        BrowserLifecycleState.LOADING,
        BrowserLifecycleState.LOBBY,
        BrowserLifecycleState.LOBBY,
        BrowserLifecycleState.ENTERING_GAME,
        BrowserLifecycleState.PLAYING,
    ]
    fake_bridge = FakeBridge(
        _state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01, lifecycle=lifecycle_sequence,
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0, lifecycle_ready_timeout_s=5.0)

    assert arm_calls == [TARGET], "Controller must arm only once PLAYING is actually reached"


def test_startup_raises_diagnostic_when_genuinely_stuck_in_lobby(monkeypatch):
    _patch_healthy_environment(monkeypatch)
    _patch_startup(monkeypatch)
    fake_bridge = FakeBridge(
        _state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01, lifecycle=BrowserLifecycleState.LOBBY,
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    with pytest.raises(bf.BrowserFarmStartupError, match="LOBBY"):
        bf.run_farming_loop(tick_interval_s=0.0, lifecycle_ready_timeout_s=0.02)


def test_startup_arms_only_after_playing_and_post_transition_oracle(monkeypatch):
    # Oracle telemetry that only arrives (or is only fresh) AFTER the
    # PLAYING transition is what unblocks arm() -- see
    # _wait_for_fresh_oracle_after.
    _patch_healthy_environment(monkeypatch)
    _patch_startup(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    fake_bridge = FakeBridge(
        _state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01,
        lifecycle=[BrowserLifecycleState.LOBBY, BrowserLifecycleState.PLAYING],
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    arm_calls: list[TargetWindow] = []
    original_arm = ctrl_mod.Controller.arm

    def recording_arm(self, target=None):
        arm_calls.append(target)
        original_arm(self, target)

    monkeypatch.setattr(ctrl_mod.Controller, "arm", recording_arm)

    bf.run_farming_loop(max_ticks=1, tick_interval_s=0.0, lifecycle_ready_timeout_s=5.0)

    assert arm_calls == [TARGET]


def test_startup_raises_when_no_oracle_telemetry_ever_arrives_after_playing(monkeypatch):
    _patch_healthy_environment(monkeypatch)
    terminate_calls = _patch_startup(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))

    fake_bridge = FakeBridge(state=None, age=None, lifecycle=BrowserLifecycleState.PLAYING)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    with pytest.raises(bf.BrowserFarmStartupError, match="no Oracle telemetry"):
        bf.run_farming_loop(tick_interval_s=0.0, oracle_after_playing_timeout_s=0.05)

    assert fake_bridge.stopped is True
    assert len(terminate_calls) == 1


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
    # ever calling bridge.latest() again inside the tick loop (readiness
    # itself legitimately calls latest() once to confirm telemetry arrived).
    _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)

    fake_bridge = FakeBridge(_state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    # Trip Controller.arm() into an immediately-disarmed state -- directly
    # simulate a post-arm async trip (what FocusWatcher._check()/
    # EmergencyStop actually do under the hood via _trip_if_epoch), which
    # is the exact scenario this regression targets.
    original_arm = ctrl_mod.Controller.arm

    def arm_then_trip(self, target=None):
        original_arm(self, target)
        self._trip_if_epoch(self._epoch, "cursor_left_target_while_button_held")

    monkeypatch.setattr(ctrl_mod.Controller, "arm", arm_then_trip)

    bf.run_farming_loop(max_ticks=1000, tick_interval_s=0.0)

    assert fake_bridge.latest_calls == 1, (
        "readiness legitimately reads telemetry once to confirm it arrived, but the loop "
        "must stop on the armed check before reading telemetry a second time"
    )


def test_run_farming_loop_prints_trip_reason_when_disarmed(monkeypatch, capsys):
    _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)

    fake_bridge = FakeBridge(_state(_shape("square", 900.0, 450.0), canvas=CANVAS), 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    original_arm = ctrl_mod.Controller.arm

    def arm_then_trip(self, target=None):
        original_arm(self, target)
        self._trip_if_epoch(self._epoch, "cursor_left_target_while_button_held")

    monkeypatch.setattr(ctrl_mod.Controller, "arm", arm_then_trip)

    bf.run_farming_loop(max_ticks=1000, tick_interval_s=0.0)

    assert "cursor_left_target_while_button_held" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# browser-lifecycle-v0: per-tick runtime gameplay gate
# ---------------------------------------------------------------------------


def _usable_state():
    return _state(_shape("square", 900.0, 450.0), canvas=CANVAS)


def _run_n_ticks(monkeypatch, sent, *, n, lifecycle, oracle_received_at=None, freeze_oracle_received_at=False):
    """`lifecycle` is a per-TICK sequence (lifecycle[0] == tick 1's
    reported state, lifecycle[1] == tick 2's, ...) -- an extra leading
    PLAYING is prepended automatically to account for the ONE
    latest_lifecycle() call startup's own readiness wait already
    consumes before the main tick loop ever runs."""
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)
    full_sequence = [BrowserLifecycleState.PLAYING] + list(lifecycle)
    fake_bridge = FakeBridge(
        _usable_state(), 0.01, lifecycle=full_sequence,
        oracle_received_at=oracle_received_at, freeze_oracle_received_at=freeze_oracle_received_at,
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)
    bf.run_farming_loop(max_ticks=n, tick_interval_s=0.0)
    return fake_bridge


def test_runtime_dead_immediately_release_all(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    # tick 1: PLAYING (fires); tick 2: DEAD (must release).
    _run_n_ticks(monkeypatch, sent, n=2, lifecycle=[BrowserLifecycleState.PLAYING, BrowserLifecycleState.DEAD])
    assert ("bdown", "left") in sent, "tick 1 (PLAYING) must have fired for this test to be meaningful"
    assert ("bup", "left") in sent, "DEAD must release the held shot"


def test_runtime_dead_prints_suspended_diagnostic(monkeypatch, capsys):
    sent = _patch_healthy_environment(monkeypatch)
    _run_n_ticks(monkeypatch, sent, n=2, lifecycle=[BrowserLifecycleState.PLAYING, BrowserLifecycleState.DEAD])
    out = capsys.readouterr().out
    assert "browser lifecycle: DEAD" in out
    assert "gameplay input: suspended" in out


def test_runtime_stale_lifecycle_immediately_release_all(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)
    # State stays PLAYING throughout, but the AGE goes stale on tick 2 --
    # must be treated identically to DEAD/UNKNOWN (immediate release_all()).
    # lifecycle_age[0] covers both startup's consumption and tick 1 (fresh,
    # fires); lifecycle_age[1] covers tick 2 (stale, must release).
    fake_bridge = FakeBridge(
        _usable_state(), 0.01,
        lifecycle=BrowserLifecycleState.PLAYING,
        lifecycle_age=[0.0, 0.0, bf.LIFECYCLE_STALE_AFTER_S + 1.0],
    )
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    bf.run_farming_loop(max_ticks=2, tick_interval_s=0.0)

    assert ("bdown", "left") in sent, "tick 1 must have fired for this test to be meaningful"
    assert ("bup", "left") in sent, "stale lifecycle telemetry must release the held shot"


def test_runtime_unknown_immediately_release_all(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    _run_n_ticks(monkeypatch, sent, n=2, lifecycle=[BrowserLifecycleState.PLAYING, BrowserLifecycleState.UNKNOWN])
    assert ("bdown", "left") in sent
    assert ("bup", "left") in sent


def test_runtime_lobby_and_entering_game_and_captcha_all_release_all(monkeypatch):
    for state in (BrowserLifecycleState.LOBBY, BrowserLifecycleState.ENTERING_GAME, BrowserLifecycleState.CAPTCHA_REQUIRED, BrowserLifecycleState.LOADING):
        sent = _patch_healthy_environment(monkeypatch)
        _run_n_ticks(monkeypatch, sent, n=2, lifecycle=[BrowserLifecycleState.PLAYING, state])
        assert ("bdown", "left") in sent, f"tick 1 must have fired for the {state} case to be meaningful"
        assert ("bup", "left") in sent, f"{state} must release the held shot"


def test_old_pre_death_oracle_snapshot_never_causes_action_after_respawn(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    # Oracle telemetry is frozen at whatever received_at its FIRST ever
    # latest() call captured (real time.monotonic(), taken during
    # startup's _wait_for_fresh_oracle_after -- so it correctly satisfies
    # the FIRST PLAYING transition) and never advances again after that --
    # as if the tab/renderer effectively stopped updating right after the
    # very first life. By the time lifecycle cycles PLAYING -> DEAD ->
    # PLAYING (a fresh respawn, tick 3), real wall-clock time has moved
    # on, so that same frozen received_at can no longer be after the
    # SECOND transition -- farming must not act on tick 3 despite
    # lifecycle already reporting PLAYING again.
    _run_n_ticks(
        monkeypatch, sent, n=3,
        lifecycle=[BrowserLifecycleState.PLAYING, BrowserLifecycleState.DEAD, BrowserLifecycleState.PLAYING],
        freeze_oracle_received_at=True,
    )

    assert ("bdown", "left") in sent, "tick 1 (first PLAYING) must have fired for this test to be meaningful"
    fire_count = sent.count(("bdown", "left"))
    assert fire_count == 1, (
        "a stale, pre-respawn Oracle snapshot must never produce a NEW gameplay action after "
        f"a fresh PLAYING transition (expected exactly 1 shot start, got {fire_count})"
    )


def test_fresh_oracle_after_new_playing_allows_action_again(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    # Same PLAYING -> DEAD -> PLAYING cycle, but this time Oracle telemetry
    # is auto-freshened on every latest() call (the default -- omit
    # freeze_oracle_received_at) -- once genuinely fresh telemetry arrives
    # after the new PLAYING transition, farming must resume and fire
    # again (a NEW press, not just the first life's still-held button).
    _run_n_ticks(
        monkeypatch, sent, n=3,
        lifecycle=[BrowserLifecycleState.PLAYING, BrowserLifecycleState.DEAD, BrowserLifecycleState.PLAYING],
    )

    fire_count = sent.count(("bdown", "left"))
    assert fire_count == 2, f"expected a fresh press on tick 1 AND again on tick 3 (post-respawn), got {fire_count}"


def test_multiple_playing_dead_playing_cycles(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    lifecycle_sequence = [
        BrowserLifecycleState.PLAYING,  # tick 1: playing
        BrowserLifecycleState.DEAD,     # tick 2: dead
        BrowserLifecycleState.PLAYING,  # tick 3: respawned, playing
        BrowserLifecycleState.DEAD,     # tick 4: dead again
        BrowserLifecycleState.PLAYING,  # tick 5: respawned again, playing
    ]
    _run_n_ticks(monkeypatch, sent, n=len(lifecycle_sequence), lifecycle=lifecycle_sequence)

    # A fresh shot on every one of the three PLAYING stretches (ticks 1, 3, 5).
    assert sent.count(("bdown", "left")) == 3
    # One release for each of the two DEAD stretches, plus one more from
    # run_farming_loop's own teardown (the loop ends mid-tick-5 still
    # actively firing -- its `finally` block always releases held input).
    assert sent.count(("bup", "left")) == 3


def test_panic_trip_during_playing_terminates_and_is_never_rearmed_by_lifecycle(monkeypatch, capsys):
    sent = _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)

    # PLAYING throughout (lifecycle never reports DEAD/anything else), but
    # Controller trips on tick 1 as if the panic key fired -- the loop
    # must stop and stay disarmed even though lifecycle keeps saying
    # PLAYING on every subsequent (never-reached) tick.
    fake_bridge = FakeBridge(_usable_state(), 0.01, lifecycle=BrowserLifecycleState.PLAYING)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    original_arm = ctrl_mod.Controller.arm

    def arm_then_trip(self, target=None):
        original_arm(self, target)
        self._trip_if_epoch(self._epoch, "emergency_stop")

    monkeypatch.setattr(ctrl_mod.Controller, "arm", arm_then_trip)

    bf.run_farming_loop(max_ticks=1000, tick_interval_s=0.0)

    out = capsys.readouterr().out
    assert "emergency_stop" in out
    assert "stopping farming loop" in out
    # Only one arm() call ever happened -- lifecycle transitions (even
    # though this fake keeps reporting PLAYING) never re-armed Controller.
    assert fake_bridge.latest_calls == 1


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


# ---------------------------------------------------------------------------
# _aim_direction / _target_candidates (projectile-speed-and-lead-v0)
# ---------------------------------------------------------------------------


def test_aim_direction_none_without_last_aim_point_or_origin():
    assert bf._aim_direction(None, (0.0, 0.0)) is None
    assert bf._aim_direction((10.0, 0.0), None) is None


def test_aim_direction_points_from_origin_to_last_aim_point():
    assert bf._aim_direction((110.0, 50.0), (100.0, 50.0)) == (10.0, 0.0)


def test_aim_direction_none_when_coincident():
    assert bf._aim_direction((100.0, 50.0), (100.0, 50.0)) is None


def test_target_candidates_excludes_claimed_and_near_self():
    origin = (800.0, 450.0)
    near_self = _circle(820.0, 450.0)  # 20px from origin, within SELF_EXCLUSION_RADIUS_PX
    claimed_far = _circle(1200.0, 450.0)
    unclaimed_far = _circle(1200.0, 460.0)
    circles = (near_self, claimed_far, unclaimed_far)

    candidates = bf._target_candidates(circles, origin, frozenset({(1200.0, 450.0)}))

    assert [(c.cx, c.cy) for c in candidates] == [(1200.0, 460.0)]


# ---------------------------------------------------------------------------
# _format_lead_diagnostics
# ---------------------------------------------------------------------------


def test_format_lead_diagnostics_unavailable_state_shows_reasons():
    from deep_eye_oh.browser_policy import LeadResult
    from deep_eye_oh.projectile_tracking import ProjectileSpeedEstimate

    output = bf._format_lead_diagnostics(
        circles_seen=0,
        projectile_tracks=0,
        likely_own_projectiles=0,
        speed_estimate=ProjectileSpeedEstimate(speed_px_s=None, confidence=0.0, sample_count=0, measured_at=0.0, last_updated=None),
        now_monotonic=0.0,
        target=None,
        now_ms=None,
        origin=None,
        lead=LeadResult(available=False, reason="no_target"),
        commanded_aim=None,
    )
    assert "circles_seen: 0" in output
    assert "bullet_speed: unavailable (insufficient_samples)" in output
    assert "target_now: unavailable (no_target)" in output
    assert "intercept: unavailable (no_target)" in output
    assert "commanded_aim: none" in output


def test_format_lead_diagnostics_available_state_shows_values():
    from deep_eye_oh.browser_policy import LeadResult
    from deep_eye_oh.projectile_tracking import ProjectileSpeedEstimate
    from deep_eye_oh.target_tracking import TargetObservation

    speed_estimate = ProjectileSpeedEstimate(speed_px_s=713.4, confidence=0.91, sample_count=12, measured_at=10.0, last_updated=9.92)
    target = TargetObservation(cx=1200.0, cy=550.0, vx=0.0, vy=200.0, radius=15.0, timestamp_ms=550.0, confidence=0.8)
    lead = LeadResult(available=True, reason="ok", aim_x=1200.0, aim_y=600.0, intercept_t=0.25)

    output = bf._format_lead_diagnostics(
        circles_seen=2,
        projectile_tracks=1,
        likely_own_projectiles=1,
        speed_estimate=speed_estimate,
        now_monotonic=10.0,
        target=target,
        now_ms=550.0,
        origin=(800.0, 450.0),
        lead=lead,
        commanded_aim=(1200, 600),
    )
    assert "bullet_speed: 713.4 px/s" in output
    assert "bullet_speed_confidence: 0.91" in output
    assert "bullet_speed_samples: 12" in output
    assert "target_now: (1200, 550)" in output
    assert "target_velocity: (0.0, 200.0) px/s" in output
    assert "target_confidence: 0.80" in output
    assert "intercept_t: 0.250 s" in output
    assert "predicted_intercept: (1200, 600)" in output
    assert "commanded_aim: (1200, 600)" in output


def _lead_scenario_states():
    """A scripted 12-tick sequence: a stationary bait shape keeps farming
    shooting every tick (so OwnProjectileTracker's shoot_active gate stays
    open); starting tick 2, a projectile circle travels at a true 700px/s
    along the aim direction (+x from self) and a target circle travels at
    200px/s in +y, far enough from self/the projectile to never be
    confused with either. By tick 12 there are 10 consistent own-
    projectile speed samples (enough for ProjectileSpeedEstimate to cross
    its confidence bar) and a fresh, confident TargetObservation -- lead
    should become available exactly there."""
    bait = _shape("square", 900.0, 450.0)
    states = [_state(bait, canvas=CANVAS, received_at=0.0)]  # tick 1: no circles yet
    for tick in range(2, 13):
        t_ms = 50.0 * (tick - 1)
        projectile = _circle(820.0 + (35.0 * (tick - 2)), 450.0, timestamp_ms=t_ms)
        moving_target = _circle(1200.0, 450.0 + (10.0 * (tick - 2)), timestamp_ms=t_ms)
        state = BrowserGameState(
            shapes=(bait,), circles=(projectile, moving_target), canvas=CANVAS,
            polled_at_ms=t_ms, performance_now_ms=t_ms, received_at=0.0,
        )
        states.append(state)
    return states


def test_run_farming_loop_wires_own_projectile_speed_and_target_lead(monkeypatch):
    sent = _patch_healthy_environment(monkeypatch)
    monkeypatch.setattr(window_focus, "client_rect_on_screen", lambda t: (0, 0, 1600, 900))
    _patch_startup(monkeypatch)

    states = _lead_scenario_states()
    # One extra leading copy of the first state: run_farming_loop's startup
    # readiness stage legitimately consumes one FakeBridge state before the
    # tick loop starts, so without this the whole scripted sequence would
    # be shifted one tick early. Age/received_at stay auto-fresh
    # throughout (the default) so this is purely about circle/shape content.
    fake_bridge = FakeBridge([states[0]] + states, 0.01)
    monkeypatch.setattr(bf, "BrowserBridgeServer", lambda port=None, lifecycle_config=None: fake_bridge)

    bf.run_farming_loop(max_ticks=len(states), tick_interval_s=0.0)

    # Early ticks (before enough own-projectile samples/target confidence
    # accumulate): farming's own shape-aim behavior must be unaffected --
    # existing farming behavior is not destroyed by adding lead.
    assert ("move", 900, 450) in sent

    # By the final tick, lead must have kicked in: the commanded aim point
    # must be the intercept solver's own answer for the exact target
    # state/estimated speed this scenario converges to (700 px/s, target
    # at (1200, 550) moving (0, 200) px/s), not the raw bait shape point.
    expected = solve_intercept((800.0, 450.0), (1200.0, 550.0), (0.0, 200.0), 700.0)
    assert expected is not None
    expected_move = ("move", round(expected.aim_x), round(expected.aim_y))
    assert expected_move in sent, f"expected {expected_move} (lead-computed) in {sent}"
    assert expected_move != ("move", 900, 450)
