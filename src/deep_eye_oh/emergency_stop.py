"""Independent emergency-stop mechanism: a GetAsyncKeyState polling watchdog
for a panic key, plus a Windows console-control-handler for Ctrl+C/Break/
close/logoff/shutdown cleanup. Does not depend on capture, vision,
GameState, policy, or any target-window classifier.
"""

from __future__ import annotations

import ctypes
from collections.abc import Callable

import win32api

from deep_eye_oh._polling_watchdog import PollingWatchdog

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_GetAsyncKeyState = _user32.GetAsyncKeyState
_GetAsyncKeyState.argtypes = (ctypes.c_int,)
_GetAsyncKeyState.restype = ctypes.c_short

_VK_CODES: dict[str, int] = {
    "pause": 0x13,
    "f9": 0x78,
}

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

_PYTHON_HANDLED_EVENTS = frozenset({CTRL_C_EVENT, CTRL_BREAK_EVENT})


def _panic_key_currently_down(vk: int) -> bool:
    """High-order 'currently down' bit only -- the low-order 'pressed since
    last call' bit is documented by Microsoft as unreliable and is
    deliberately not used. A zero return is not proof the key is safely up
    (documented UIPI/secure-desktop blind spot) -- covered by the
    independent focus/cursor checks in window_focus.py instead."""
    return bool(_GetAsyncKeyState(vk) & 0x8000)


class EmergencyStop:
    """Independent panic-key watchdog + console-control-handler.

    is_triggered() is a latch: once the panic key is detected, it stays
    True (and on_trip is called exactly once for that trigger) regardless
    of the physical key being released afterward. Only reset() (called
    only from Controller.arm()) clears it.
    """

    def __init__(
        self,
        on_trip: Callable[[str], None],
        panic_key: str = "pause",
        interval_s: float = 0.02,
    ) -> None:
        try:
            self._vk = _VK_CODES[panic_key]
        except KeyError:
            raise ValueError(f"unknown panic key {panic_key!r}") from None
        self._on_trip = on_trip
        self._triggered = False
        self._console_handler = self._handle_console_event
        self._console_handler_registered = False
        self._watchdog = PollingWatchdog(
            check=self._check,
            interval_s=interval_s,
            on_thread_failure=lambda: self._on_trip("emergency_stop_watchdog_died"),
            name="EmergencyStop",
        )

    def _check(self) -> None:
        if _panic_key_currently_down(self._vk) and not self._triggered:
            self._triggered = True
            self._on_trip("emergency_stop")

    def _handle_console_event(self, ctrl_type: int) -> bool:
        try:
            self._on_trip("console_control_event")
        except Exception:
            pass
        # Ctrl+C/Ctrl+Break: never swallow the signal -- return False so it
        # continues to Python's own handler and normal interruption still
        # happens. Close/logoff/shutdown: nothing else needs to run.
        return ctrl_type not in _PYTHON_HANDLED_EVENTS

    def start(self) -> None:
        self._watchdog.start()
        win32api.SetConsoleCtrlHandler(self._console_handler, True)
        self._console_handler_registered = True

    def stop(self, timeout: float | None = None) -> None:
        self._watchdog.stop(timeout=timeout)
        if self._console_handler_registered:
            win32api.SetConsoleCtrlHandler(self._console_handler, False)
            self._console_handler_registered = False

    def is_alive(self) -> bool:
        return self._watchdog.is_alive()

    def is_triggered(self) -> bool:
        return self._triggered

    def reset(self) -> None:
        self._triggered = False

    def trigger(self) -> None:
        if not self._triggered:
            self._triggered = True
            self._on_trip("emergency_stop")
