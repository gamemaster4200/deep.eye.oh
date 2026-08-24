"""Foreground-window and cursor-target validation, and asynchronous
focus/cursor monitoring. The only module importing win32gui/win32process.

Never used for input injection (see win32_input.py). Any failure or
ambiguous result from any query in this module is fail-closed (returns
False), never assumed safe.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Callable
from dataclasses import dataclass

import win32api
import win32gui
import win32process

from deep_eye_oh._polling_watchdog import PollingWatchdog

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_VK_MENU = 0x12  # Alt
_KEYEVENTF_KEYUP = 0x0002

GA_ROOT = 2


@dataclass(frozen=True)
class TargetWindow:
    """An armed window, fingerprinted by hwnd + owning pid (not title --
    titles are ambiguous/spoofable; the pid closes the narrow window where
    Windows reuses an hwnd value for a different, unrelated window)."""

    hwnd: int
    pid: int
    title_at_arm: str


def arm_foreground_window() -> TargetWindow:
    """Capture whatever window is currently foreground, right now.

    No countdown here -- that is an operator-facing CLI concern layered on
    top of this simple, immediate, testable primitive.
    """
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        raise RuntimeError("no foreground window to arm")
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    title = win32gui.GetWindowText(hwnd)
    return TargetWindow(hwnd=hwnd, pid=pid, title_at_arm=title)


def _windows_for_pid(pid: int) -> list[int]:
    """Visible, titled top-level windows owned by the given pid."""
    matches: list[int] = []

    def _callback(hwnd: int, _extra: object) -> bool:
        if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd):
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
            if window_pid == pid:
                matches.append(hwnd)
        return True

    win32gui.EnumWindows(_callback, None)
    return matches


def _nudge_own_foreground_eligibility() -> None:
    """Windows only allows a process to call SetForegroundWindow (or grant
    it via AllowSetForegroundWindow) if that process itself is currently
    considered to have "received the last input event" -- true for a
    terminal a human just pressed Enter in, but not for this process when
    it was spawned by a non-interactive launcher with no real input
    provenance of its own (observed directly during implementation: a
    plain Popen-spawned Chrome's window could not be foregrounded at all
    from such a caller, even with AllowSetForegroundWindow +
    AttachThreadInput, until this was added). A synthetic, harmless Alt
    key-down/up tap -- a well-established, widely used workaround, not an
    officially documented API contract -- makes this process itself count
    as having just generated input, which is sufficient to regain
    foreground-setting eligibility. Never raises; this is best-effort."""
    try:
        _user32.keybd_event(_VK_MENU, 0, 0, 0)
        _user32.keybd_event(_VK_MENU, 0, _KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _force_foreground(hwnd: int) -> None:
    """Best-effort SetForegroundWindow for a window owned by a process we
    just spawned (not the caller's own foreground window), against
    Windows' focus-stealing prevention -- the standard
    AllowSetForegroundWindow / AttachThreadInput pattern, plus a
    self-eligibility nudge (see _nudge_own_foreground_eligibility)."""
    _nudge_own_foreground_eligibility()
    _, target_pid = win32process.GetWindowThreadProcessId(hwnd)
    try:
        # pywin32 does not wrap AllowSetForegroundWindow in any of its
        # win32api/win32gui/win32process modules -- call user32 directly,
        # matching win32_input.py's existing ctypes-fallback pattern for
        # Win32 APIs pywin32 doesn't cover.
        _user32.AllowSetForegroundWindow(target_pid)
    except Exception:
        pass
    try:
        win32gui.SetForegroundWindow(hwnd)
        return
    except Exception:
        pass

    # Fallback: attach our input thread to the currently-foreground
    # window's thread so Windows treats the SetForegroundWindow call as
    # coming from the currently-focused thread, which it always permits.
    current_thread = win32api.GetCurrentThreadId()
    foreground_hwnd = win32gui.GetForegroundWindow()
    foreground_thread = (
        win32process.GetWindowThreadProcessId(foreground_hwnd)[0] if foreground_hwnd else 0
    )
    attached = False
    try:
        if foreground_thread and foreground_thread != current_thread:
            win32process.AttachThreadInput(current_thread, foreground_thread, True)
            attached = True
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            win32process.AttachThreadInput(current_thread, foreground_thread, False)


def arm_process_window(pid: int, timeout_s: float = 15.0, *, poll_interval_s: float = 0.1) -> TargetWindow:
    """Locate the top-level window owned by a just-spawned process (by
    pid), force it to the foreground, and arm it.

    Used by browser-farm for its launched Chrome for Testing window, which
    cannot rely on arm_foreground_window()'s "whatever is foreground right
    now" behavior -- a freshly spawned process becoming foreground is not
    guaranteed by the OS. Raises RuntimeError on timeout rather than
    silently arming the wrong window.
    """
    deadline = time.monotonic() + timeout_s
    hwnd: int | None = None
    while time.monotonic() < deadline:
        candidates = _windows_for_pid(pid)
        if candidates:
            hwnd = candidates[0]
            break
        time.sleep(poll_interval_s)
    if hwnd is None:
        raise RuntimeError(f"no top-level window appeared for process {pid} within {timeout_s:.0f}s")

    _force_foreground(hwnd)

    confirmed = False
    while time.monotonic() < deadline:
        if win32gui.GetForegroundWindow() == hwnd:
            confirmed = True
            break
        time.sleep(poll_interval_s)
    if not confirmed:
        raise RuntimeError(f"could not bring process {pid}'s window to the foreground within {timeout_s:.0f}s")

    _, owning_pid = win32process.GetWindowThreadProcessId(hwnd)
    title = win32gui.GetWindowText(hwnd)
    return TargetWindow(hwnd=hwnd, pid=owning_pid, title_at_arm=title)


def target_still_exists(target: TargetWindow) -> bool:
    """A lightweight existence-only sanity check (used by Controller.arm()
    before investing effort starting watchdogs) -- weaker than
    is_foreground, which additionally requires current foreground+pid."""
    try:
        return bool(win32gui.IsWindow(target.hwnd))
    except Exception:
        return False


def is_foreground(target: TargetWindow) -> bool:
    """Three-check fail-closed: window still exists, is the current
    foreground window, and its current owning pid still matches the pid
    recorded at arm time (closes the hwnd-reuse-after-close gap)."""
    try:
        if not win32gui.IsWindow(target.hwnd):
            return False
        if win32gui.GetForegroundWindow() != target.hwnd:
            return False
        _, pid = win32process.GetWindowThreadProcessId(target.hwnd)
        if pid != target.pid:
            return False
    except Exception:
        return False
    return True


def client_rect_on_screen(target: TargetWindow) -> tuple[int, int, int, int] | None:
    """The target window's client-area rectangle in physical screen pixels
    (left, top, width, height), or None on any failure -- fail-closed, like
    every other query in this module. Used by browser-informed-farming-v0's
    coordinate transform (browser_game_state.py) to map Oracle canvas
    pixels onto physical screen points, via the same win32gui coordinate
    space win32_input.send_mouse_move ultimately targets (see that
    module's docstring on the unvalidated-DPI-equivalence caveat this
    still shares)."""
    try:
        if not win32gui.IsWindow(target.hwnd):
            return None
        left, top, right, bottom = win32gui.GetClientRect(target.hwnd)
        screen_left, screen_top = win32gui.ClientToScreen(target.hwnd, (left, top))
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None
    except Exception:
        return None
    return (screen_left, screen_top, width, height)


def point_is_over_target(target: TargetWindow, x: int, y: int) -> bool:
    """Does the given physical pixel point resolve to the armed top-level
    window? WindowFromPoint can return a child control, so this walks up
    to the top-level owner (GA_ROOT) before comparing."""
    try:
        if not win32gui.IsWindow(target.hwnd):
            return False
        hwnd = win32gui.WindowFromPoint((x, y))
        if not hwnd:
            return False
        root = win32gui.GetAncestor(hwnd, GA_ROOT)
        if not root:
            return False
        if root != target.hwnd:
            return False
    except Exception:
        return False
    return True


def cursor_is_over_target(target: TargetWindow) -> bool:
    try:
        x, y = win32gui.GetCursorPos()
    except Exception:
        return False
    return point_is_over_target(target, x, y)


class FocusWatcher:
    """Background focus/cursor monitor for one armed session.

    Every tick: checks is_foreground(target) unconditionally, tripping via
    on_trip("focus_lost_async") on failure. Additionally, only while
    has_held_buttons() reports True, also checks cursor_is_over_target
    (session-level monitoring shared with the focus check -- not a thread
    per click), tripping via on_trip("cursor_left_target_while_button_held")
    on failure. Fails closed (on_trip("focus_watcher_died")) on its own
    thread death too.
    """

    def __init__(
        self,
        target: TargetWindow,
        has_held_buttons: Callable[[], bool],
        on_trip: Callable[[str], None],
        interval_s: float = 0.02,
    ) -> None:
        self.target = target
        self._has_held_buttons = has_held_buttons
        self._on_trip = on_trip
        self._watchdog = PollingWatchdog(
            check=self._check,
            interval_s=interval_s,
            on_thread_failure=lambda: self._on_trip("focus_watcher_died"),
            name="FocusWatcher",
        )

    def _check(self) -> None:
        if not is_foreground(self.target):
            self._on_trip("focus_lost_async")
            return
        if self._has_held_buttons() and not cursor_is_over_target(self.target):
            self._on_trip("cursor_left_target_while_button_held")

    def start(self) -> None:
        self._watchdog.start()

    def stop(self, timeout: float | None = None) -> None:
        self._watchdog.stop(timeout=timeout)

    def is_alive(self) -> bool:
        return self._watchdog.is_alive()
