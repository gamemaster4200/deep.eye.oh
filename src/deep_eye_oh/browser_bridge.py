"""Local WebSocket server: the deep.eye.oh side of the thin bridge to
deep.eye.oh.ext's background service worker
(extension/background/bridge.js). Accepts connections, receives JSON
`oracle_snapshot` messages, and exposes the most recently successfully
parsed BrowserGameState plus its age -- nothing more.

browser-lifecycle-v0 narrows (does not remove) the "no inbound command
channel" invariant: the extension now also sends `bridge_hello` (once per
connection) and `lifecycle_snapshot` messages, and this server replies to
`bridge_hello` with exactly one validated `lifecycle_config` message
(player name + game mode) -- see browser_lifecycle.py. No other inbound
message type is ever acted on, and Python never sends anything except that
one config reply. The Oracle `latest()` slot and the new lifecycle slot are
independent: a lifecycle message can never overwrite/corrupt Oracle state
or vice versa.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

from websockets.sync.server import Server, serve

from deep_eye_oh.browser_game_state import BrowserGameState, InvalidSnapshotError, parse_bridge_message
from deep_eye_oh.browser_lifecycle import (
    BrowserFarmConfig,
    BrowserLifecycleSnapshot,
    InvalidBridgeHelloError,
    InvalidLifecycleMessageError,
    build_lifecycle_config_message,
    parse_lifecycle_message,
    validate_bridge_hello,
)

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
        lifecycle_config: BrowserFarmConfig | None = None,
    ) -> None:
        self._port = port
        self._time_source = time_source
        self._lifecycle_config = lifecycle_config or BrowserFarmConfig()
        self._lock = threading.Lock()
        self._latest: BrowserGameState | None = None
        self._latest_lifecycle: BrowserLifecycleSnapshot | None = None
        self._connected = False
        self._server: Server | None = None
        self._thread: threading.Thread | None = None

    def _handle_connection(self, connection) -> None:
        with self._lock:
            self._connected = True
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

            elif message_type == "lifecycle_snapshot":
                try:
                    snapshot = parse_lifecycle_message(raw, received_at=received_at)
                except InvalidLifecycleMessageError as exc:
                    logger.warning("dropping malformed lifecycle message: %s", exc)
                    continue
                with self._lock:
                    self._latest_lifecycle = snapshot

            elif message_type == "bridge_hello":
                try:
                    validate_bridge_hello(raw)
                except InvalidBridgeHelloError as exc:
                    logger.warning("dropping malformed bridge_hello: %s", exc)
                    continue
                with self._lock:
                    config = self._lifecycle_config
                try:
                    # Sent from this connection-handler context (see this
                    # slice's PR description) -- no separate outbound-
                    # message queue/thread is needed for the one narrow
                    # reply this server ever sends.
                    connection.send(json.dumps(build_lifecycle_config_message(config)))
                except Exception:
                    logger.warning("failed to send lifecycle_config in response to bridge_hello", exc_info=True)

            else:
                logger.warning("dropping unrecognized bridge message type: %r", message_type)

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

    def latest_lifecycle(self) -> BrowserLifecycleSnapshot | None:
        """A second, independent latest-state slot for lifecycle
        telemetry -- never overwritten by or overwriting the Oracle
        `latest()` slot above (see module docstring)."""
        with self._lock:
            return self._latest_lifecycle

    def lifecycle_age_s(self, now: float | None = None) -> float | None:
        snapshot = self.latest_lifecycle()
        if snapshot is None:
            return None
        current = now if now is not None else self._time_source()
        return current - snapshot.received_at

    def __enter__(self) -> "BrowserBridgeServer":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
