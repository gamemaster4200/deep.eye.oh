"""Local WebSocket server: the deep.eye.oh side of the thin bridge to
deep.eye.oh.ext's background service worker
(extension/background/bridge.js). Accepts connections, receives JSON
`oracle_snapshot` messages, and exposes the most recently successfully
parsed BrowserGameState plus its age -- nothing more.

There is no inbound command channel FROM this server back to the browser:
the extension only ever sends, never receives, over this connection (see
extension/background/bridge.js's own doc comment). This module does not
send anything either.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from websockets.sync.server import Server, serve

from deep_eye_oh.browser_game_state import BrowserGameState, InvalidSnapshotError, parse_bridge_message

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
    ) -> None:
        self._port = port
        self._time_source = time_source
        self._lock = threading.Lock()
        self._latest: BrowserGameState | None = None
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    def _handle_connection(self, connection) -> None:
        for raw_text in connection:
            received_at = self._time_source()
            try:
                raw = json.loads(raw_text)
                state = parse_bridge_message(raw, received_at=received_at)
            except (json.JSONDecodeError, InvalidSnapshotError) as exc:
                logger.warning("dropping malformed bridge message: %s", exc)
                continue
            with self._lock:
                self._latest = state

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
        if self._server is not None:
            self._server.shutdown()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None

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

    def __enter__(self) -> "BrowserBridgeServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
