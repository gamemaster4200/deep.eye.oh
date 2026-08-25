"""Local WebSocket server: the deep.eye.oh side of the thin bridge to
deep.eye.oh.ext's background service worker
(extension/background/bridge.js). Accepts connections, receives JSON
messages, and exposes the most recently successfully parsed
BrowserGameState plus its age -- the primary, highest-volume traffic on
this connection is still the one-way `oracle_snapshot` telemetry stream
this module has always carried.

browser-overlay-control-v0 adds a narrow, explicit, reviewed exception to
this connection's previous one-way-only design (see CLAUDE.md's
browser-overlay-control-v0 section and deep.eye.oh.ext's AGENTS.md for the
full rationale): two more inbound message types --
`overlay_command` (a user-typed text command, queued via pop_commands())
and `overlay_focus` (drives PhysicalKeyboardCapture's lifecycle, see
physical_keyboard_hook.py) -- and three outbound message types this
server can now send back over the SAME connection: `overlay_command_result`,
`bot_status`, and `overlay_key_event`. This module still never interprets
overlay_command text itself (see overlay_command.py's dispatch_command)
and never simulates any game input -- that stays exclusively Controller's
job (control.py).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from websockets.sync.server import Server, serve

from deep_eye_oh.browser_game_state import BrowserGameState, InvalidSnapshotError, parse_bridge_message
from deep_eye_oh.overlay_command import CommandResult, InvalidOverlayCommandError, OverlayCommand, parse_overlay_command
from deep_eye_oh.physical_keyboard_hook import KeyEvent, PhysicalKeyboardCapture

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8765


class BrowserBridgeServer:
    """Runs a local WebSocket server on a background thread. A malformed
    or undecodable message is logged and dropped -- it never raises into
    the serving thread and never overwrites the last good state, so an
    operator/loop sees "telemetry has gone stale" rather than a silent
    corruption or a crash."""

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        *,
        time_source: Callable[[], float] = time.monotonic,
        physical_keyboard_capture: PhysicalKeyboardCapture | None = None,
    ) -> None:
        self._port = port
        self._time_source = time_source
        self._lock = threading.Lock()
        self._latest: BrowserGameState | None = None
        self._connected = False
        self._server: Server | None = None
        self._thread: threading.Thread | None = None
        self._connection = None
        self._pending_commands: list[OverlayCommand] = []
        # Injectable so tests never have to install a real Windows global
        # hook (see physical_keyboard_hook.py) -- constructing the real
        # one is itself side-effect-free (no OS call happens before
        # start()), so this is a safe default outside tests.
        self._physical_keyboard = physical_keyboard_capture or PhysicalKeyboardCapture(
            on_key_event=self.push_key_event
        )

    def _handle_connection(self, connection) -> None:
        with self._lock:
            self._connected = True
            self._connection = connection
        try:
            for raw_text in connection:
                received_at = self._time_source()
                try:
                    raw = json.loads(raw_text)
                except json.JSONDecodeError as exc:
                    logger.warning("dropping malformed bridge message: %s", exc)
                    continue
                message_type = raw.get("type") if isinstance(raw, dict) else None

                if message_type == "oracle_snapshot":
                    try:
                        state = parse_bridge_message(raw, received_at=received_at)
                    except InvalidSnapshotError as exc:
                        logger.warning("dropping malformed bridge message: %s", exc)
                        continue
                    with self._lock:
                        self._latest = state
                elif message_type == "overlay_command":
                    try:
                        command = parse_overlay_command(raw, received_at=received_at)
                    except InvalidOverlayCommandError as exc:
                        logger.warning("dropping malformed overlay_command message: %s", exc)
                        continue
                    with self._lock:
                        self._pending_commands.append(command)
                elif message_type == "overlay_focus":
                    focused = raw.get("focused")
                    if not isinstance(focused, bool):
                        logger.warning("dropping malformed overlay_focus message: %r", raw)
                        continue
                    if focused:
                        self._physical_keyboard.start()
                    else:
                        self._physical_keyboard.stop()
                else:
                    logger.warning("dropping bridge message of unknown type: %r", message_type)
        finally:
            with self._lock:
                if self._connection is connection:
                    self._connection = None
            # Never leave physical keyboard input suppressed across a
            # dropped connection -- overlay_focus:false may simply never
            # arrive if the tab/bridge dies while the overlay had focus.
            self._physical_keyboard.stop()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("BrowserBridgeServer is already started")
        self._server = serve(self._handle_connection, "127.0.0.1", self._port)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="BrowserBridgeServer", daemon=True
        )
        self._thread.start()

    @property
    def port(self) -> int:
        """The actually-bound port -- resolves port=0 (OS-assigned) to the
        real value once started; used by tests to avoid fixed-port
        collisions."""
        if self._server is not None:
            return self._server.socket.getsockname()[1]
        return self._port

    def stop(self, timeout: float | None = None) -> None:
        self._physical_keyboard.stop()
        with self._lock:
            self._connection = None
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None

    def has_connected(self) -> bool:
        """True once at least one WebSocket connection has been accepted,
        regardless of whether it has sent any (valid) message yet -- a
        cheaper, earlier readiness signal than latest() being non-None, so
        startup orchestration can distinguish "extension never connected"
        from "extension connected but the Oracle isn't producing telemetry
        yet"."""
        with self._lock:
            return self._connected

    def latest(self) -> BrowserGameState | None:
        with self._lock:
            return self._latest

    def age_s(self, now: float | None = None) -> float | None:
        """Seconds since the last successfully parsed message was
        received, or None if none has ever arrived."""
        state = self.latest()
        if state is None:
            return None
        current = now if now is not None else self._time_source()
        return current - state.received_at

    def pop_commands(self) -> list[OverlayCommand]:
        """Drains and returns every overlay_command received since the
        last call, preserving arrival order. Never touches latest()'s
        telemetry slot -- a separate, independent piece of state."""
        with self._lock:
            commands = list(self._pending_commands)
            self._pending_commands.clear()
        return commands

    def _send(self, payload: dict) -> None:
        """Best-effort push over whichever connection is currently active
        -- silently a no-op with no active connection, and never raises
        into the caller on a send failure (matching this module's existing
        fail-closed/never-raise style), since browser_farming.py's loop
        must never be interrupted by a telemetry/result push failing."""
        with self._lock:
            connection = self._connection
        if connection is None:
            return
        try:
            connection.send(json.dumps(payload))
        except Exception:  # noqa: BLE001 -- best-effort push only
            logger.warning("failed to send bridge message (type=%r)", payload.get("type"))

    def send_command_result(self, command: OverlayCommand, result: CommandResult) -> None:
        self._send(
            {
                "type": "overlay_command_result",
                "text": command.text,
                "status": result.status,
                "message": result.message,
            }
        )

    def push_status(self, status: dict) -> None:
        self._send({"type": "bot_status", **status})

    def push_key_event(self, event: KeyEvent) -> None:
        self._send({"type": "overlay_key_event", "kind": event.kind, "value": event.value})

    def __enter__(self) -> "BrowserBridgeServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
