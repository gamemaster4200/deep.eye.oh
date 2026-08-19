"""Controller: the only module anything outside the control subsystem may
import for keyboard/mouse control. Wraps win32_input.py, window_focus.py,
and emergency_stop.py behind one safety-gated facade.

Safety invariant (structural, not conventional):

    not armed OR wrong focus OR wrong cursor target (mouse buttons only)
        OR emergency stop OR control failure
            -> NO input

`Controller.armed` is the single thing `_gate_locked()` checks. Every
failure source -- an emergency-stop trigger, an async focus/cursor-target
loss, a synchronous focus/cursor check failing at send time, either
watchdog thread being unexpectedly dead, or a SendInput call itself
failing -- funnels through one method, `_apply_trip_locked(reason)`, which
sets `armed = False`, records `_trip_reason`, and best-effort releases
every held key/button (never forgetting a failed release -- a release that
fails stays tracked and is retried on the next release_all()).

Latching: regaining focus, releasing the panic key, or the cursor
returning to the target window does NOT auto-resume input. Only an
explicit arm() clears a trip.

Generation/epoch model: `self._epoch` is a monotonically increasing
counter bumped by every shutdown() (including the one arm() always
performs first, and any concurrent disarm()). Every EmergencyStop/
FocusWatcher created by arm() has its trip callback closed over the exact
epoch that was current when it was created. `_trip_if_epoch(epoch, reason)`
-- the only entry point watchdog/console-handler callbacks use -- is a
no-op if that epoch no longer matches `self._epoch`, so a stale watchdog
from an old, superseded arm attempt can never trip a newer, unrelated,
already-armed session. arm() itself only commits `armed = True` after
verifying, in one atomic lock acquisition, that nothing changed the epoch
and nothing tripped since it started (see arm()'s docstring).

Concurrency: a single `threading.Lock` (`self._lock`, plain, not
reentrant by convention) protects `armed`, `_trip_reason`, `_target`,
`_held_keys`, `_held_buttons`, `_epoch`. Only outer public/semi-public
methods acquire it; internal `*_locked` helpers assume it is already held
and never re-acquire it. Lock-protected work and watcher-thread-lifecycle
work are strictly separated: marking unsafe + releasing input happens
under the lock; `.stop()`/`.join()`-ing watcher threads always happens
*outside* the lock (a watcher thread mid-flight through `_trip_if_epoch`
needs to be able to acquire the lock and finish before its own poll loop
notices a stop request -- stopping it while holding the lock it needs
would deadlock). Compound operations (tap_key/click) never hold the lock
across their dwell sleep, so a safety trip can always acquire the lock
promptly even while a key/button is temporarily held.

Cleanup coverage, precisely:
  - Strongly covered (release-all reliably runs): normal exit of a
    `with Controller(...)` block; an exception unwinding through it;
    Ctrl+C/Ctrl+Break (the console handler runs cleanup then returns False
    so Python's own KeyboardInterrupt handling still runs afterward --
    Ctrl+C is never swallowed); the panic key.
  - Best-effort, not guaranteed: console close (X button), logoff,
    shutdown, via SetConsoleCtrlHandler within Windows' ~5s handler
    budget -- not claimed guaranteed given documented extra limitations
    for interactive apps once user32/gdi32 are loaded (this process loads
    both).
  - Not covered, fundamentally uncatchable at the process level: Task
    Manager "End Task"/taskkill /F/TerminateProcess; a hard crash;
    os._exit(); power loss -- same class as SIGKILL. No further
    mitigation is attempted for these.

Known v0 limitation: mouse coordinates are not demonstrated to be
DPI-scaling-equivalent to capture (deep_eye_oh.capture) pixel coordinates.
See win32_input.py's module docstring.

Replay + input events: deliberately out of scope here. Capture and control
are two separate, non-integrated capabilities in this slice -- there is no
real combined capture+control loop yet to validate an input-event replay
schema against. A natural, cheap future extension once one exists: a
second per-event JSONL stream keyed by the same monotonic clock
Observation already uses (deep_eye_oh.observation), not a change to
frames.jsonl's existing per-frame record.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time

from deep_eye_oh import window_focus
from deep_eye_oh import win32_input
from deep_eye_oh.emergency_stop import EmergencyStop
from deep_eye_oh.window_focus import FocusWatcher, TargetWindow

logger = logging.getLogger(__name__)

DEFAULT_DWELL_S = 0.05


class ControlNotSafeError(RuntimeError):
    """Raised when an input-sending call is refused by the safety gate."""


class ControlArmFailedError(RuntimeError):
    """Raised when arm() cannot establish a safe, healthy session."""


class Controller:
    def __init__(self, panic_key: str = "pause") -> None:
        self._panic_key = panic_key
        self.armed = False
        self._trip_reason: str | None = None
        self._target: TargetWindow | None = None
        self._held_keys: set[str] = set()
        self._held_buttons: set[str] = set()
        self._lock = threading.Lock()
        self._epoch = 0
        self._emergency_stop: EmergencyStop | None = None
        self._focus_watcher: FocusWatcher | None = None
        self._atexit_registered = False

    # ---- lifecycle ----------------------------------------------------

    def arm(self, target: TargetWindow | None = None) -> None:
        """Arm on `target` (or whatever window is currently foreground, if
        omitted). Transactional: commits `armed = True` only after (a) any
        prior session is fully retired, (b) no previously-owned key/button
        remains unreleased (a failed release from shutdown() is a hard
        refusal -- ControlArmFailedError("unreleased_input"), no new
        watchdog infrastructure is started), (c) a fresh EmergencyStop and
        FocusWatcher for this target are started and report healthy, (d)
        nothing tripped during that startup window, and (e) a final
        synchronous foreground check passes -- all reverified atomically
        under the lock at commit time. Watchdog construction/start is
        itself a rollback-on-any-failure transaction (including
        KeyboardInterrupt/BaseException): whatever got started before a
        failure is stopped before the exception propagates. Raises
        ControlArmFailedError (or propagates whatever startup raised) and
        leaves the Controller disarmed, with no leaked watcher threads, if
        any of that fails.
        """
        if target is None:
            target = window_focus.arm_foreground_window()

        self.shutdown()  # fully retire any prior session first

        with self._lock:
            my_epoch = self._epoch
            self._trip_reason = None
            unreleased_input = bool(self._held_keys or self._held_buttons)

        if unreleased_input:
            # Some previously-owned key/button failed to release during
            # shutdown() and is still tracked (possibly still physically
            # held). Starting a new session on top of that is unsafe --
            # fail closed, start no new watchdog infrastructure. A later
            # release_all()/arm() retry can succeed once the release does.
            raise ControlArmFailedError("unreleased_input")

        if not window_focus.target_still_exists(target):
            raise ControlArmFailedError("target window no longer exists")

        # Startup transaction: if anything raises (including
        # KeyboardInterrupt/other BaseException) while constructing or
        # starting the watchdogs, roll back whatever was already started
        # before the exception propagates -- EmergencyStop.start() starts
        # its polling thread before registering the console handler, so a
        # registration failure can leave that thread already live.
        new_estop: EmergencyStop | None = None
        new_focus_watcher: FocusWatcher | None = None
        startup_ok = False
        try:
            new_estop = EmergencyStop(
                on_trip=lambda reason: self._trip_if_epoch(my_epoch, reason),
                panic_key=self._panic_key,
            )
            new_estop.start()

            new_focus_watcher = FocusWatcher(
                target=target,
                has_held_buttons=self._has_held_buttons,
                on_trip=lambda reason: self._trip_if_epoch(my_epoch, reason),
            )
            new_focus_watcher.start()
            startup_ok = True
        finally:
            if not startup_ok:
                for watchdog in (new_focus_watcher, new_estop):
                    if watchdog is None:
                        continue
                    try:
                        watchdog.stop()
                    except Exception:
                        logger.warning(
                            "failed to stop watchdog during arm() rollback", exc_info=True
                        )

        committed = False
        failure_reason: str | None = None
        with self._lock:
            if (
                self._epoch == my_epoch
                and self._trip_reason is None
                and new_estop.is_alive()
                and new_focus_watcher.is_alive()
                and not new_estop.is_triggered()
                and window_focus.is_foreground(target)
            ):
                self.armed = True
                self._target = target
                self._emergency_stop = new_estop
                self._focus_watcher = new_focus_watcher
                self._trip_reason = None
                committed = True
            else:
                failure_reason = self._trip_reason or "arm_health_check_failed"

        if not committed:
            new_estop.stop()
            new_focus_watcher.stop()
            raise ControlArmFailedError(failure_reason)

        if not self._atexit_registered:
            atexit.register(self.shutdown)
            self._atexit_registered = True

    def disarm(self) -> None:
        """Voluntary, non-failure end of a session."""
        self.shutdown()

    def shutdown(self) -> None:
        """The shared full-teardown routine disarm(), arm()'s own
        retirement step, and __exit__ all use. Bumps the epoch (so any
        in-flight arm() attempt or stale watchdog callback is invalidated),
        marks unsafe, releases held input, then -- outside the lock --
        stops/joins any existing watchdogs. Safe to call when already
        disarmed."""
        with self._lock:
            self._epoch += 1
            self.armed = False
            self._trip_reason = None
            self._release_all_locked()
            old_estop, self._emergency_stop = self._emergency_stop, None
            old_focus_watcher, self._focus_watcher = self._focus_watcher, None
            self._target = None
        if old_estop is not None:
            old_estop.stop()
        if old_focus_watcher is not None:
            old_focus_watcher.stop()

    def _has_held_buttons(self) -> bool:
        with self._lock:
            return bool(self._held_buttons)

    # ---- trip machinery -------------------------------------------------

    def _trip_if_epoch(self, epoch: int, reason: str) -> None:
        """External entry point for watchdog/console-handler callbacks.
        No-op if `epoch` no longer matches the current generation (a stale
        callback from a superseded arm attempt or session)."""
        with self._lock:
            if epoch != self._epoch:
                return
            self._apply_trip_locked(reason)

    def _apply_trip_locked(self, reason: str) -> None:
        self.armed = False
        self._trip_reason = reason
        self._release_all_locked()

    def _gate_locked(
        self,
        *,
        require_cursor_over_target: bool = False,
        require_point_over_target: tuple[int, int] | None = None,
    ) -> None:
        """Order: armed -> both watchdogs alive -> emergency-stop not
        already triggered -> focus valid (sync recheck) -> cursor/point
        rule, if this call requires one. Any failure applies the trip and
        raises ControlNotSafeError."""
        if not self.armed:
            raise ControlNotSafeError(self._trip_reason or "not armed")
        if self._emergency_stop is None or self._focus_watcher is None:
            self._apply_trip_locked("watchdog_missing")
            raise ControlNotSafeError("watchdog_missing")
        if not self._emergency_stop.is_alive() or not self._focus_watcher.is_alive():
            self._apply_trip_locked("watchdog_dead")
            raise ControlNotSafeError("watchdog_dead")
        if self._emergency_stop.is_triggered():
            self._apply_trip_locked("emergency_stop")
            raise ControlNotSafeError("emergency_stop")
        if not window_focus.is_foreground(self._target):
            self._apply_trip_locked("focus_lost_sync")
            raise ControlNotSafeError("focus_lost_sync")
        if require_cursor_over_target and not window_focus.cursor_is_over_target(self._target):
            self._apply_trip_locked("cursor_not_over_target")
            raise ControlNotSafeError("cursor_not_over_target")
        if require_point_over_target is not None:
            x, y = require_point_over_target
            if not window_focus.point_is_over_target(self._target, x, y):
                self._apply_trip_locked("move_destination_not_over_target")
                raise ControlNotSafeError("move_destination_not_over_target")

    # ---- keyboard -------------------------------------------------------

    def press_key(self, name: str) -> int:
        """Returns the epoch this press succeeded under (see tap_key)."""
        with self._lock:
            self._gate_locked()
            try:
                win32_input.send_key_down(name)
            except Exception:
                self._apply_trip_locked("send_failed")
                raise
            self._held_keys.add(name)
            return self._epoch

    def _release_key_locked(self, name: str) -> None:
        """Assumes lock held. No-op if not currently tracked as held.
        Never raises -- a failed release stays tracked for retry."""
        if name not in self._held_keys:
            return
        try:
            win32_input.send_key_up(name)
        except Exception:
            logger.warning("failed to release key %r (left tracked for retry)", name)
            return
        self._held_keys.discard(name)

    def release_key(self, name: str) -> None:
        """Gate-exempt: releasing input must never be refused."""
        with self._lock:
            self._release_key_locked(name)

    def _release_key_if_epoch(self, name: str, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                return  # stale delayed release from a superseded generation
            self._release_key_locked(name)

    def tap_key(self, name: str, dwell_s: float = DEFAULT_DWELL_S) -> None:
        epoch = self.press_key(name)
        time.sleep(dwell_s)  # lock NOT held during the dwell
        self._release_key_if_epoch(name, epoch)

    # ---- mouse ------------------------------------------------------------

    def move_mouse(self, x: int, y: int) -> None:
        """Unrestricted (beyond the ordinary focus gate) unless a button is
        currently held, in which case the destination point must resolve
        to the armed target window -- the async cursor watcher (§10)
        catches physical drift, but must not be the first thing to notice
        an unsafe move Controller itself generates."""
        with self._lock:
            point_check = (x, y) if self._held_buttons else None
            self._gate_locked(require_point_over_target=point_check)
            try:
                win32_input.send_mouse_move(x, y)
            except Exception:
                self._apply_trip_locked("send_failed")
                raise

    def press_button(self, button: str = "left") -> int:
        with self._lock:
            self._gate_locked(require_cursor_over_target=True)
            try:
                win32_input.send_mouse_button_down(button)
            except Exception:
                self._apply_trip_locked("send_failed")
                raise
            self._held_buttons.add(button)
            return self._epoch

    def _release_button_locked(self, button: str) -> None:
        if button not in self._held_buttons:
            return
        try:
            win32_input.send_mouse_button_up(button)
        except Exception:
            logger.warning("failed to release button %r (left tracked for retry)", button)
            return
        self._held_buttons.discard(button)

    def release_button(self, button: str = "left") -> None:
        with self._lock:
            self._release_button_locked(button)

    def _release_button_if_epoch(self, button: str, epoch: int) -> None:
        with self._lock:
            if epoch != self._epoch:
                return
            self._release_button_locked(button)

    def click(self, button: str = "left", dwell_s: float = DEFAULT_DWELL_S) -> None:
        epoch = self.press_button(button)
        time.sleep(dwell_s)
        self._release_button_if_epoch(button, epoch)

    # ---- release-all --------------------------------------------------

    def release_all(self) -> None:
        """Always callable, regardless of armed state. Never raises."""
        with self._lock:
            self._release_all_locked()

    def _release_all_locked(self) -> None:
        for name in list(self._held_keys):
            self._release_key_locked(name)
        for button in list(self._held_buttons):
            self._release_button_locked(button)

    # ---- context manager ------------------------------------------------

    def __enter__(self) -> "Controller":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.shutdown()
