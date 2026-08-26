"""Integration tests for browser_bridge.py: a real local WebSocket server
on an OS-assigned ephemeral port, exercised with a real websockets sync
client -- fast, fully local, no external network or manual server."""

import json
import time

import pytest
from websockets.sync.client import connect

from deep_eye_oh.browser_bridge import BrowserBridgeServer
from deep_eye_oh.browser_lifecycle import BrowserFarmConfig, BrowserLifecycleState


def _valid_raw_message(**overrides):
    message = {
        "type": "oracle_snapshot",
        "tabId": 1,
        "polledAtMs": 5.0,
        "snapshot": {"shapes": []},
    }
    message.update(overrides)
    return message


def _valid_lifecycle_message(**overrides):
    message = {
        "type": "lifecycle_snapshot",
        "tabId": 1,
        "observedAtMs": 5.0,
        "snapshot": {"state": "LOBBY", "reason": "home_screen_ready", "selectedMode": "ffa"},
    }
    message.update(overrides)
    return message


def _valid_hello(**overrides):
    message = {"type": "bridge_hello", "protocolVersion": 1, "capabilities": ["oracle_snapshot", "lifecycle_v0"]}
    message.update(overrides)
    return message


@pytest.fixture
def server():
    clock = {"value": 0.0}
    srv = BrowserBridgeServer(port=0, time_source=lambda: clock["value"])
    srv.start()
    srv.clock = clock  # test-only convenience handle
    try:
        yield srv
    finally:
        srv.stop(timeout=5)


def _connect(server):
    return connect(f"ws://127.0.0.1:{server.port}/")


def test_latest_is_none_before_any_message(server):
    assert server.latest() is None
    assert server.age_s() is None


def test_has_connected_false_before_any_connection(server):
    assert server.has_connected() is False


def test_has_connected_true_once_connection_accepted_before_any_message(server):
    # has_connected() must go True as soon as the WebSocket handshake
    # completes, independent of whether any (valid) message ever arrives --
    # it's the earlier, cheaper readiness signal browser-farm's startup
    # orchestration checks before waiting on latest().
    with _connect(server) as client:
        _wait_until(lambda: server.has_connected())
        assert server.latest() is None, "has_connected() must not require a message to have been sent yet"


def test_receives_and_parses_a_valid_message(server):
    server.clock["value"] = 10.0
    with _connect(server) as client:
        client.send(json.dumps(_valid_raw_message(polledAtMs=42.0)))
        _wait_until(lambda: server.latest() is not None)

    state = server.latest()
    assert state is not None
    assert state.polled_at_ms == 42.0
    assert state.shapes == ()
    assert state.received_at == 10.0


def test_age_s_reflects_time_source(server):
    server.clock["value"] = 100.0
    with _connect(server) as client:
        client.send(json.dumps(_valid_raw_message()))
        _wait_until(lambda: server.latest() is not None)

    server.clock["value"] = 100.4
    assert server.age_s() == pytest.approx(0.4)
    assert server.age_s(now=101.0) == pytest.approx(1.0)


def test_malformed_json_is_dropped_not_raised(server):
    with _connect(server) as client:
        client.send("not valid json{{{")
        client.send(json.dumps(_valid_raw_message(polledAtMs=99.0)))
        _wait_until(lambda: server.latest() is not None)

    # the connection must still be usable after a bad message, and the
    # next good message must still be recorded.
    assert server.latest().polled_at_ms == 99.0


def test_invalid_snapshot_is_dropped_and_does_not_clobber_last_good_state(server):
    with _connect(server) as client:
        client.send(json.dumps(_valid_raw_message(polledAtMs=1.0)))
        _wait_until(lambda: server.latest() is not None)

        bad = _valid_raw_message(polledAtMs=2.0)
        bad["snapshot"]["shapes"] = [{"class": "square"}]  # missing cx/cy/radius/timestamp
        client.send(json.dumps(bad))
        time.sleep(0.2)  # give the bad message a chance to (not) apply

    assert server.latest().polled_at_ms == 1.0, "a malformed message must not overwrite the last good state"


def test_accepts_a_large_oracle_snapshot_from_a_busy_match(server):
    # Regression: a real busy match's shapes/circles arrays can legitimately
    # exceed the `websockets` library's own 1 MiB default frame-size limit
    # -- live-smoke-confirmed to otherwise silently drop the connection
    # (taking lifecycle_snapshot down with it on the same socket) and
    # produce spurious LOBBY/stale-telemetry flicker during ordinary,
    # dense gameplay. This must be accepted, not dropped/disconnected.
    big_shapes = [{"class": "square", "cx": float(i), "cy": 0.0, "radius": 1.0, "timestamp": 0.0} for i in range(30000)]
    with _connect(server) as client:
        client.send(json.dumps(_valid_raw_message(polledAtMs=1.0, snapshot={"shapes": big_shapes})))
        _wait_until(lambda: server.latest() is not None)

    assert server.latest().polled_at_ms == 1.0
    assert len(server.latest().shapes) == 30000


def test_multiple_messages_keep_only_the_latest(server):
    with _connect(server) as client:
        for i in range(5):
            client.send(json.dumps(_valid_raw_message(polledAtMs=float(i))))
        _wait_until(lambda: server.latest() is not None and server.latest().polled_at_ms == 4.0)

    assert server.latest().polled_at_ms == 4.0


def test_stop_before_start_and_double_stop_are_safe():
    srv = BrowserBridgeServer(port=0)
    srv.stop()  # never started
    srv.start()
    srv.stop(timeout=5)
    srv.stop(timeout=5)  # already stopped


def test_start_twice_raises():
    srv = BrowserBridgeServer(port=0)
    srv.start()
    try:
        with pytest.raises(RuntimeError):
            srv.start()
    finally:
        srv.stop(timeout=5)


def test_context_manager():
    with BrowserBridgeServer(port=0) as srv:
        with _connect(srv) as client:
            client.send(json.dumps(_valid_raw_message()))
            _wait_until(lambda: srv.latest() is not None)
        assert srv.latest() is not None


# ---------------------------------------------------------------------------
# browser-lifecycle-v0: lifecycle slot is independent from the Oracle slot
# ---------------------------------------------------------------------------


def test_latest_lifecycle_is_none_before_any_message(server):
    assert server.latest_lifecycle() is None
    assert server.lifecycle_age_s() is None


def test_receives_and_parses_a_valid_lifecycle_message(server):
    server.clock["value"] = 20.0
    with _connect(server) as client:
        client.send(json.dumps(_valid_lifecycle_message(observedAtMs=77.0)))
        _wait_until(lambda: server.latest_lifecycle() is not None)

    snapshot = server.latest_lifecycle()
    assert snapshot.state is BrowserLifecycleState.LOBBY
    assert snapshot.reason == "home_screen_ready"
    assert snapshot.selected_mode == "ffa"
    assert snapshot.received_at == 20.0


def test_lifecycle_age_s_reflects_time_source(server):
    server.clock["value"] = 100.0
    with _connect(server) as client:
        client.send(json.dumps(_valid_lifecycle_message()))
        _wait_until(lambda: server.latest_lifecycle() is not None)

    server.clock["value"] = 100.3
    assert server.lifecycle_age_s() == pytest.approx(0.3)


def test_malformed_lifecycle_message_is_dropped_not_raised(server):
    with _connect(server) as client:
        bad = _valid_lifecycle_message()
        bad["snapshot"]["state"] = "NOT_A_REAL_STATE"
        client.send(json.dumps(bad))
        client.send(json.dumps(_valid_lifecycle_message(observedAtMs=99.0)))
        _wait_until(lambda: server.latest_lifecycle() is not None)

    assert server.latest_lifecycle().reason == "home_screen_ready"


def test_lifecycle_message_never_overwrites_oracle_slot_and_vice_versa(server):
    with _connect(server) as client:
        client.send(json.dumps(_valid_raw_message(polledAtMs=1.0)))
        client.send(json.dumps(_valid_lifecycle_message(observedAtMs=2.0)))
        _wait_until(lambda: server.latest() is not None and server.latest_lifecycle() is not None)

    assert server.latest().polled_at_ms == 1.0
    assert server.latest_lifecycle().state is BrowserLifecycleState.LOBBY

    with _connect(server) as client2:
        bad_oracle = _valid_raw_message(polledAtMs=999.0)
        bad_oracle["snapshot"]["shapes"] = [{"class": "square"}]  # malformed
        client2.send(json.dumps(bad_oracle))
        bad_lifecycle = _valid_lifecycle_message(observedAtMs=999.0)
        bad_lifecycle["snapshot"]["state"] = "BOGUS"
        client2.send(json.dumps(bad_lifecycle))
        time.sleep(0.2)

    # Neither malformed message (of either kind) corrupted the other slot.
    assert server.latest().polled_at_ms == 1.0
    assert server.latest_lifecycle().state is BrowserLifecycleState.LOBBY


def test_unknown_message_type_is_dropped_not_raised(server):
    with _connect(server) as client:
        client.send(json.dumps({"type": "run_command", "command": "rm -rf /"}))
        client.send(json.dumps(_valid_raw_message(polledAtMs=1.0)))
        _wait_until(lambda: server.latest() is not None)

    assert server.latest().polled_at_ms == 1.0
    assert server.latest_lifecycle() is None


# ---------------------------------------------------------------------------
# browser-lifecycle-v0: bridge_hello -> lifecycle_config reply
# ---------------------------------------------------------------------------


def test_bridge_hello_gets_lifecycle_config_reply():
    config = BrowserFarmConfig(player_name="deep.eye.oh", game_mode="teams")
    srv = BrowserBridgeServer(port=0, lifecycle_config=config)
    srv.start()
    try:
        with _connect(srv) as client:
            client.send(json.dumps(_valid_hello()))
            reply = json.loads(client.recv(timeout=2.0))
        assert reply == {"type": "lifecycle_config", "playerName": "deep.eye.oh", "gameMode": "teams"}
    finally:
        srv.stop(timeout=5)


def test_bridge_hello_default_config_when_none_supplied():
    srv = BrowserBridgeServer(port=0)
    srv.start()
    try:
        with _connect(srv) as client:
            client.send(json.dumps(_valid_hello()))
            reply = json.loads(client.recv(timeout=2.0))
        assert reply == {"type": "lifecycle_config", "playerName": "deep.eye.oh", "gameMode": "ffa"}
    finally:
        srv.stop(timeout=5)


def test_reconnect_gets_lifecycle_config_again(server):
    with _connect(server) as client1:
        client1.send(json.dumps(_valid_hello()))
        reply1 = json.loads(client1.recv(timeout=2.0))
    assert reply1["type"] == "lifecycle_config"

    # A second, independent connection (simulating extension reconnect)
    # gets its own fresh reply -- not merely a leftover from the first.
    with _connect(server) as client2:
        client2.send(json.dumps(_valid_hello()))
        reply2 = json.loads(client2.recv(timeout=2.0))
    assert reply2 == reply1


def test_malformed_bridge_hello_gets_no_reply_and_does_not_crash(server):
    with _connect(server) as client:
        client.send(json.dumps({"type": "bridge_hello", "protocolVersion": 999, "capabilities": []}))
        # No reply should arrive for an invalid hello -- confirm the
        # connection is still alive/usable afterward by sending a normal
        # oracle_snapshot and getting it recorded.
        client.send(json.dumps(_valid_raw_message(polledAtMs=1.0)))
        _wait_until(lambda: server.latest() is not None)
    assert server.latest().polled_at_ms == 1.0


def test_only_lifecycle_config_type_is_ever_accepted_inbound(server):
    # Any other inbound message TYPE (a generic selector/JS/shell-command/
    # action payload, or even a well-formed-looking oracle_snapshot sent
    # FROM the agent side by mistake) must never be treated as config and
    # must never crash the connection.
    with _connect(server) as client:
        client.send(json.dumps({"type": "run_js", "code": "alert(1)"}))
        client.send(json.dumps({"type": "click", "selector": "#anything"}))
        client.send(json.dumps(_valid_raw_message(polledAtMs=1.0)))
        _wait_until(lambda: server.latest() is not None)
    assert server.latest().polled_at_ms == 1.0


# ---------------------------------------------------------------------------
# overlay-control-center-v0: overlay_command / overlay_focus / pushes
# ---------------------------------------------------------------------------


class _FakePhysicalKeyboardCapture:
    """Stands in for the real Windows global hook (see
    physical_keyboard_hook.py) so these fast integration tests never touch
    a real OS hook -- only start()/stop() call-count behavior matters
    here."""

    def __init__(self):
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1

    def stop(self, timeout=None):
        self.stop_calls += 1


@pytest.fixture
def server_with_fake_hook():
    clock = {"value": 0.0}
    fake_hook = _FakePhysicalKeyboardCapture()
    srv = BrowserBridgeServer(port=0, time_source=lambda: clock["value"], physical_keyboard_capture=fake_hook)
    srv.start()
    srv.clock = clock
    srv.fake_hook = fake_hook
    try:
        yield srv
    finally:
        srv.stop(timeout=5)


def _overlay_command_message(text="pause"):
    return {"type": "overlay_command", "tabId": 1, "sentAtMs": 5.0, "text": text}


def test_overlay_command_is_queued_and_does_not_touch_latest(server):
    with _connect(server) as client:
        client.send(json.dumps(_overlay_command_message("mode farm")))
        time.sleep(0.2)  # give the message a chance to arrive and be queued

    commands = server.pop_commands()
    assert [c.text for c in commands] == ["mode farm"]
    assert server.latest() is None, "an overlay_command message must never populate the telemetry slot"
    assert server.latest_lifecycle() is None, "an overlay_command message must never populate the lifecycle slot"


def test_pop_commands_drains_in_order_and_empties(server):
    with _connect(server) as client:
        client.send(json.dumps(_overlay_command_message("pause")))
        client.send(json.dumps(_overlay_command_message("resume")))
        time.sleep(0.2)

    commands = server.pop_commands()
    assert [c.text for c in commands] == ["pause", "resume"]
    assert server.pop_commands() == []


def test_malformed_overlay_command_is_dropped_not_raised(server):
    with _connect(server) as client:
        client.send(json.dumps({"type": "overlay_command", "text": 123}))  # text must be a string
        client.send(json.dumps(_overlay_command_message("pause")))
        time.sleep(0.2)

    commands = server.pop_commands()
    assert [c.text for c in commands] == ["pause"]


def test_pushes_are_silent_no_ops_without_an_active_connection(server):
    from deep_eye_oh.overlay_command import CommandResult, OverlayCommand
    from deep_eye_oh.physical_keyboard_hook import KeyEvent

    # No connection has ever been made -- these must not raise.
    server.send_command_result(OverlayCommand(text="pause", received_at=0.0), CommandResult(status="ok", message="bot paused"))
    server.push_status({"connected": False})
    server.push_key_event(KeyEvent(kind="char", value="w"))


def test_send_command_result_delivers_over_the_active_connection(server):
    from deep_eye_oh.overlay_command import CommandResult, OverlayCommand

    with _connect(server) as client:
        client.send(json.dumps(_overlay_command_message("pause")))
        time.sleep(0.1)
        server.send_command_result(
            OverlayCommand(text="pause", received_at=0.0), CommandResult(status="ok", message="bot paused")
        )
        received = json.loads(client.recv(timeout=2.0))

    assert received == {"type": "overlay_command_result", "text": "pause", "status": "ok", "message": "bot paused"}


def test_push_status_delivers_over_the_active_connection(server):
    with _connect(server) as client:
        time.sleep(0.1)
        server.push_status({"connected": True, "pausedByCommand": False})
        received = json.loads(client.recv(timeout=2.0))

    assert received == {"type": "bot_status", "connected": True, "pausedByCommand": False}


def test_overlay_focus_true_starts_physical_keyboard_capture(server_with_fake_hook):
    with _connect(server_with_fake_hook) as client:
        client.send(json.dumps({"type": "overlay_focus", "focused": True}))
        _wait_until(lambda: server_with_fake_hook.fake_hook.start_calls == 1)

    assert server_with_fake_hook.fake_hook.start_calls == 1


def test_overlay_focus_false_stops_physical_keyboard_capture(server_with_fake_hook):
    with _connect(server_with_fake_hook) as client:
        client.send(json.dumps({"type": "overlay_focus", "focused": True}))
        _wait_until(lambda: server_with_fake_hook.fake_hook.start_calls == 1)
        client.send(json.dumps({"type": "overlay_focus", "focused": False}))
        _wait_until(lambda: server_with_fake_hook.fake_hook.stop_calls >= 1)

    assert server_with_fake_hook.fake_hook.start_calls == 1


def test_dropped_connection_stops_physical_keyboard_capture(server_with_fake_hook):
    # Safety: overlay_focus:false may simply never arrive if the tab/bridge
    # dies while the overlay had focus -- the connection dropping alone
    # must still stop the hook (never leave physical input suppressed).
    with _connect(server_with_fake_hook) as client:
        client.send(json.dumps({"type": "overlay_focus", "focused": True}))
        _wait_until(lambda: server_with_fake_hook.fake_hook.start_calls == 1)

    _wait_until(lambda: server_with_fake_hook.fake_hook.stop_calls >= 1)


def test_malformed_overlay_focus_is_dropped_not_raised(server_with_fake_hook):
    with _connect(server_with_fake_hook) as client:
        client.send(json.dumps({"type": "overlay_focus", "focused": "yes"}))  # must be a bool
        client.send(json.dumps(_overlay_command_message("pause")))
        time.sleep(0.2)

    assert server_with_fake_hook.fake_hook.start_calls == 0
    assert [c.text for c in server_with_fake_hook.pop_commands()] == ["pause"]


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition was not met within timeout"
