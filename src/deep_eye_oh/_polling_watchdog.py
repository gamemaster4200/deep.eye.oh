"""Shared background-polling-thread lifecycle, used by EmergencyStop and
FocusWatcher. Not part of the public module surface -- an internal helper.
"""

from __future__ import annotations

import threading
from collections.abc import Callable


class PollingWatchdog:
    """Runs `check()` on a background thread roughly every `interval_s`.

    If `check()` raises, the watchdog marks itself dead and calls
    `on_thread_failure()` -- fails closed on its own failure rather than
    dying silently while a caller still believes it's being monitored.
    """

    def __init__(
        self,
        check: Callable[[], None],
        interval_s: float,
        on_thread_failure: Callable[[], None],
        name: str,
    ) -> None:
        self._check = check
        self._interval_s = interval_s
        self._on_thread_failure = on_thread_failure
        self._stop_event = threading.Event()
        self._alive = False
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> None:
        self._alive = True
        self._thread.start()

    def _run(self) -> None:
        failure = False
        try:
            while True:
                self._check()
                if self._stop_event.wait(self._interval_s):
                    break
        except Exception:
            failure = True
        self._alive = False
        if failure:
            try:
                self._on_thread_failure()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self._alive

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)
