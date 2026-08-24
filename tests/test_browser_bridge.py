"""Integration tests for browser_bridge.py: a real local WebSocket server
on an OS-assigned ephemeral port, exercised with a real websockets sync
client -- fast, fully local, no external network or manual server."""

import json
import time

import pytest
from websockets.sync.client import connect

from deep_eye_oh.browser_bridge import BrowserBridgeServer


def _valid_raw_message(**overrides):
    message = {
        "type": "oracle_snapshot",
        "tabId": 1,
        "polledAtMs": 5.0,
        "snapshot": {"shapes": []},
    }
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


def _wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    assert predicate(), "condition was not met within timeout"
