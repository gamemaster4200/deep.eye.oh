"""Manual, interactive Windows control smoke-test scenario runner.

Not part of the automated test suite -- exercises real SendInput, real
window focus, the real cursor, and the real panic key against a
human-selected safe target window (e.g. Notepad).

Scenario selection happens here, in this menu loop, while the process is
disarmed and the terminal has focus. Each selected scenario then arms and
runs to completion *autonomously* (including any waits for a physical
operator action that doesn't require terminal focus -- alt-tabbing away,
or hitting the panic key, which works regardless of focus) before
returning control to this menu. Typing further commands into an *armed*
session is not supported: doing so would require switching terminal focus
away from the target window, which would itself (correctly) trip
FocusWatcher before the intended command ever ran.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Callable

from deep_eye_oh.control import Controller, ControlNotSafeError
from deep_eye_oh.window_focus import arm_foreground_window

_user32 = ctypes.windll.user32
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79


def _countdown(seconds: float, message: str) -> None:
    print(message)
    remaining = seconds
    step = 0.5
    while remaining > 0:
        print(f"  arming in {remaining:.1f}s...")
        time.sleep(min(step, remaining))
        remaining -= step


def _arm(controller: Controller, countdown_s: float) -> None:
    _countdown(countdown_s, "Switch to the target window now.")
    target = arm_foreground_window()
    controller.arm(target)
    print(f"Armed on: {target.title_at_arm!r} (hwnd={target.hwnd}, pid={target.pid})")


def _wait_while_armed(controller: Controller, timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline and controller.armed:
        time.sleep(0.1)


def scenario_tap_w(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        c.tap_key("w")
        print("Sent one tap of 'w'. Check the target window for exactly one 'w'.")
    finally:
        c.shutdown()


def scenario_tap_wasd(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        for key in ("w", "a", "s", "d"):
            c.tap_key(key)
        print("Sent w, a, s, d in sequence. Check the target window for 'wasd'.")
    finally:
        c.shutdown()


def scenario_move_mouse_multi_monitor(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        vx = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        vw = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        vh = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        points = [
            (vx + 10, vy + 10),
            (vx + vw - 10, vy + 10),
            (vx + vw // 2, vy + vh // 2),
            (vx + 10, vy + vh - 10),
            (vx + vw - 10, vy + vh - 10),
        ]
        for x, y in points:
            print(f"moving to ({x}, {y})")
            c.move_mouse(x, y)
            time.sleep(0.6)
        print("Moved across the virtual desktop's corners/center/edges. Visually confirm.")
    finally:
        c.shutdown()


def scenario_click(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        print("Clicking at the current cursor position (should be over the target)...")
        try:
            c.click("left")
            print("Click sent successfully.")
        except ControlNotSafeError as e:
            print(f"Click refused: {e}")
        print(
            "Now move the cursor over a DIFFERENT window within 5s "
            "(without alt-tabbing) -- a second click attempt will follow."
        )
        time.sleep(5)
        try:
            c.click("left")
            print("UNEXPECTED: click was sent even though cursor should be elsewhere.")
        except ControlNotSafeError as e:
            print(f"Correctly refused (cursor not over target): {e}")
    finally:
        c.shutdown()


def scenario_hold_click_then_drift_cursor(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        c.press_button("left")
        print(
            "Holding left button. Physically drag the cursor OFF the "
            "target window now (do not alt-tab). Waiting up to 10s..."
        )
        _wait_while_armed(c, 10)
        print(f"armed={c.armed} (expected False if the cursor left the target)")
    finally:
        c.shutdown()


def scenario_wrong_focus_refusal(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        print("Switch away from the target window now (do not re-arm). Waiting 5s...")
        time.sleep(5)
        try:
            c.tap_key("w")
            print("UNEXPECTED: input was sent despite wrong focus.")
        except ControlNotSafeError as e:
            print(f"Correctly refused: {e}")
        print("Now switch back to the target window. Waiting 5s...")
        time.sleep(5)
        try:
            c.tap_key("w")
            print("UNEXPECTED: input was sent after regaining focus without re-arming.")
        except ControlNotSafeError as e:
            print(f"Correctly still refused (latch persists): {e}")
        print(f"armed={c.armed}")
    finally:
        c.shutdown()


def scenario_async_focus_loss_while_idle(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        c.press_key("w")
        print(
            "Holding 'w'. Switch away from the target window now and do "
            "nothing else. Waiting up to 10s..."
        )
        _wait_while_armed(c, 10)
        print(f"armed={c.armed} (expected False -- released on its own, no further command issued)")
    finally:
        c.shutdown()


def scenario_emergency_stop(countdown_s: float = 5.0) -> None:
    c = Controller()
    try:
        _arm(c, countdown_s)
        c.press_key("w")
        print("Holding 'w'. Hit the panic key (Pause/Break) now. Waiting up to 10s...")
        _wait_while_armed(c, 10)
        print(f"armed={c.armed} (expected False)")
        try:
            c.tap_key("w")
            print("UNEXPECTED: input was sent after the panic-key trip.")
        except ControlNotSafeError as e:
            print(f"Correctly still refused (latch persists): {e}")
    finally:
        c.shutdown()


_SCENARIOS: dict[str, Callable[[], None]] = {
    "tap-w": scenario_tap_w,
    "tap-wasd": scenario_tap_wasd,
    "move-mouse-multi-monitor": scenario_move_mouse_multi_monitor,
    "click": scenario_click,
    "hold-click-then-drift-cursor": scenario_hold_click_then_drift_cursor,
    "wrong-focus-refusal": scenario_wrong_focus_refusal,
    "async-focus-loss-while-idle": scenario_async_focus_loss_while_idle,
    "emergency-stop": scenario_emergency_stop,
}


def run_menu() -> None:
    names = list(_SCENARIOS)
    while True:
        print()
        print("Control smoke-test scenarios (disarmed -- safe to have terminal focus):")
        for i, name in enumerate(names, start=1):
            print(f"  {i}) {name}")
        print(f"  {len(names) + 1}) quit")
        print("(Ctrl+C here terminates the runner -- this is the manual Ctrl+C sanity check.)")
        choice = input("> ").strip()
        if choice in ("q", "quit", str(len(names) + 1)):
            return
        try:
            index = int(choice) - 1
            if index < 0:
                raise ValueError
            name = names[index]
        except (ValueError, IndexError):
            print("invalid choice")
            continue
        print(f"--- running scenario: {name} ---")
        try:
            _SCENARIOS[name]()
        except Exception as e:  # noqa: BLE001 -- report and return to the menu, never crash the runner
            print(f"scenario raised an unexpected exception: {e!r}")
        print(f"--- scenario {name} complete ---")


if __name__ == "__main__":
    run_menu()
