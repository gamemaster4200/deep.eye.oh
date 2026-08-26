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


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition was not met within timeout"
