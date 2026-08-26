"""Integration tests for overlay_dev_backend.py: the standalone, generic
mock backend for the overlay's command/status protocol -- no Controller,
no browser, no diep.io involved anywhere in this file."""

import json
import threading

from websockets.sync.client import connect

from deep_eye_oh.overlay_dev_backend import run_overlay_dev_backend


def _start_backend(**kwargs):
    """Runs run_overlay_dev_backend on a background thread with an
    OS-assigned port, returning (thread, port) once the server is
    actually listening -- no sleep-based polling or port-guessing race."""
    ready = threading.Event()
    state = {}

    def _on_ready(bridge):
        state["port"] = bridge.port
        ready.set()

    def _target():
        run_overlay_dev_backend(port=0, on_ready=_on_ready, **kwargs)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    assert ready.wait(timeout=5.0), "overlay dev backend did not start listening in time"
    return thread, state["port"]


def test_pause_resume_round_trip_over_the_real_protocol():
    thread, port = _start_backend(tick_interval_s=0.01, status_push_every_n_ticks=5, max_ticks=500)

    with connect(f"ws://127.0.0.1:{port}/") as client:
        client.send(json.dumps({"type": "overlay_command", "tabId": 1, "sentAtMs": 0.0, "text": "pause"}))
        result = json.loads(client.recv(timeout=2.0))
        assert result == {"type": "overlay_command_result", "text": "pause", "status": "ok", "message": "bot paused"}

        status = json.loads(client.recv(timeout=2.0))
        assert status["type"] == "bot_status"
        assert status["pausedByCommand"] is True
        assert status["connected"] is True

        client.send(json.dumps({"type": "overlay_command", "tabId": 1, "sentAtMs": 0.0, "text": "resume"}))
        result = json.loads(client.recv(timeout=2.0))
        assert result["status"] == "ok"
        assert result["message"] == "bot resumed"

    thread.join(timeout=5)


def test_unsupported_command_gets_a_normal_unsupported_reply():
    thread, port = _start_backend(tick_interval_s=0.01, status_push_every_n_ticks=1000, max_ticks=500)

    with connect(f"ws://127.0.0.1:{port}/") as client:
        client.send(json.dumps({"type": "overlay_command", "tabId": 1, "sentAtMs": 0.0, "text": "mode farm"}))
        result = json.loads(client.recv(timeout=2.0))
        assert result["status"] == "unsupported"

    thread.join(timeout=5)
