"""overlay-control-center-v0: a standalone, generic mock backend for
BrowserBridgeServer's overlay_command/overlay_focus/bot_status channel --
used to exercise the full protocol (Python <-> WebSocket <-> extension
Shadow DOM overlay) for manual dev-testing and automated integration
tests, WITHOUT any dependency on Controller, browser_farming.py, or any
other live-gameplay/actuation code.

This is intentionally NOT run_farming_loop wired up to the overlay: the
Control Center slice's architectural boundary is "ears + language +
instrumentation UI" -- it must not make game decisions and does not
require a live-game backend to be useful/testable at all (see
CLAUDE.md's overlay-control-center-v0 section). dispatch_command() is
still the pure, generic parser/router from overlay_command.py; the only
thing this module adds is a trivial IN-MEMORY pause/resume flag and a
periodic synthetic status push -- both obviously non-gameplay (no target,
no aim, no bullet speed, nothing this module cannot honestly claim to
know), so nothing here can be mistaken for real bot telemetry.

A future backend (the real farming loop, or a simulator/private-sandbox
implementation) can replace this module entirely without any change to
BrowserBridgeServer, overlay_command.py, or the extension side: all three
already only depend on the generic pop_commands()/send_command_result()/
push_status() surface this module also uses.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from deep_eye_oh.browser_bridge import DEFAULT_PORT, BrowserBridgeServer
from deep_eye_oh.overlay_command import dispatch_command

logger = logging.getLogger(__name__)

TICK_INTERVAL_S = 0.1
STATUS_PUSH_EVERY_N_TICKS = 10  # ~1s at the default 10Hz tick rate


def run_overlay_dev_backend(
    *,
    port: int = DEFAULT_PORT,
    tick_interval_s: float = TICK_INTERVAL_S,
    status_push_every_n_ticks: int = STATUS_PUSH_EVERY_N_TICKS,
    max_ticks: int | None = None,
    on_ready: Callable[[BrowserBridgeServer], None] | None = None,
) -> None:
    """Starts a BrowserBridgeServer and services its overlay channel
    forever (or for max_ticks, for tests): drains pop_commands() each
    tick, dispatches each through the same pure dispatch_command() the
    real bot side would use, tracks an in-memory `paused` flag purely for
    demonstrating the pause/resume round-trip, and periodically pushes a
    synthetic bot_status. Never touches Controller, a real browser, or
    diep.io -- this is a protocol-level dev/test harness only. Runs until
    max_ticks (None = forever / until Ctrl+C). `on_ready`, if given, is
    called once with the started server (e.g. so a test can read its
    OS-assigned `.port` synchronously instead of polling/sleeping)."""
    bridge = BrowserBridgeServer(port=port)
    bridge.start()
    print(f"overlay dev backend listening on ws://127.0.0.1:{bridge.port}/ (mock -- no live game attached)")
    if on_ready is not None:
        on_ready(bridge)
    paused = False
    tick = 0
    try:
        while max_ticks is None or tick < max_ticks:
            tick += 1
            for command in bridge.pop_commands():
                result = dispatch_command(command)
                if result.effect == "pause":
                    paused = True
                elif result.effect == "resume":
                    paused = False
                bridge.send_command_result(command, result)
                print(f"overlay command: {command.text!r} -> {result.status} ({result.message})")

            if tick % status_push_every_n_ticks == 0:
                bridge.push_status(
                    {
                        "connected": bridge.has_connected(),
                        "pausedByCommand": paused,
                        "tickCount": tick,
                    }
                )

            time.sleep(tick_interval_s)
    finally:
        bridge.stop()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_overlay_dev_backend()
