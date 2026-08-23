"""Foreground-window and cursor-target validation, and asynchronous
focus/cursor monitoring. The only module importing win32gui/win32process.

Never used for input injection (see win32_input.py). Any failure or
ambiguous result from any query in this module is fail-closed (returns
False), never assumed safe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import win32gui
import win32process

from deep_eye_oh._polling_watchdog import PollingWatchdog

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


def client_area_origin_on_screen(target: TargetWindow) -> tuple[int, int]:
    """Physical-pixel screen coordinates of `target`'s client-area origin
    (ClientToScreen(hwnd, (0, 0))) -- physical pixels only if the calling
    process is per-monitor-DPI-aware (see win32_input.ensure_dpi_awareness,
    GitHub issue #2); otherwise a Windows-virtualized, non-physical space.

    For a browser window this is the top-level WINDOW's client-area
    origin, which on modern (Aura) Chrome includes the tab strip/toolbars
    -- not just the page viewport. Callers wanting the page viewport's
    origin must additionally account for the browser chrome offset (see
    deep_eye_oh.browser_coords.BrowserGeometry.chrome_offset_top_css /
    chrome_offset_left_right_css).

    Unlike the boolean checks in this module, this is a geometry lookup,
    not a safety check -- it propagates whatever win32gui.ClientToScreen
    raises (e.g. pywintypes.error) if `target`'s hwnd is no longer valid.
    Callers wanting fail-closed behavior should check
    target_still_exists()/is_foreground() first, same as elsewhere.
    """
    return win32gui.ClientToScreen(target.hwnd, (0, 0))


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
