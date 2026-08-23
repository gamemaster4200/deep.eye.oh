"""Live, manual-trigger verification that browser_coords' transform lands
the real cursor on the same shape the Browser Oracle reports.

Never sends a click. Never holds a movement key -- a single move_mouse()
call, then shutdown(). Reuses the existing arm/focus/emergency-stop
machinery exactly as smoke_test.py does; no new safety mechanism is
introduced here.

Input is a small, flat JSON object captured fresh from a live diep.io tab
by the companion browser-side probe (deep.eye.oh.ext) -- not a persisted
calibration file. Expected fields:

    cx, cy,                                            (Oracle canvas point)
    canvasWidth, canvasHeight,
    canvasRectLeft, canvasRectTop, canvasRectWidth, canvasRectHeight,
    devicePixelRatio,
    innerWidth, innerHeight, outerWidth, outerHeight

Any missing/invalid field fails closed (raises CoordinateGateError /
ValueError before anything is armed or moved) rather than guessing.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from deep_eye_oh.browser_coords import BrowserGeometry, browser_point_to_controller_point
from deep_eye_oh.control import Controller, ControlNotSafeError
from deep_eye_oh.window_focus import arm_foreground_window, client_area_origin_on_screen

_REQUIRED_FIELDS = (
    "cx",
    "cy",
    "canvasWidth",
    "canvasHeight",
    "canvasRectLeft",
    "canvasRectTop",
    "canvasRectWidth",
    "canvasRectHeight",
    "devicePixelRatio",
    "innerWidth",
    "innerHeight",
    "outerWidth",
    "outerHeight",
)


class CoordinateGateError(RuntimeError):
    """Missing/invalid input, or an unsafe state -- always fail closed."""


def load_payload(source: str) -> dict:
    """`source` is a file path, or "-" for stdin."""
    try:
        text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except OSError as exc:
        raise CoordinateGateError(f"could not read {source!r}: {exc}") from None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CoordinateGateError(f"invalid JSON: {exc}") from None
    missing = [field for field in _REQUIRED_FIELDS if field not in payload]
    if missing:
        raise CoordinateGateError(f"payload missing required field(s): {', '.join(missing)}")
    return payload


def geometry_from_payload(payload: dict) -> BrowserGeometry:
    try:
        return BrowserGeometry(
            canvas_width=payload["canvasWidth"],
            canvas_height=payload["canvasHeight"],
            canvas_rect_left=payload["canvasRectLeft"],
            canvas_rect_top=payload["canvasRectTop"],
            canvas_rect_width=payload["canvasRectWidth"],
            canvas_rect_height=payload["canvasRectHeight"],
            device_pixel_ratio=payload["devicePixelRatio"],
            inner_width=payload["innerWidth"],
            inner_height=payload["innerHeight"],
            outer_width=payload["outerWidth"],
            outer_height=payload["outerHeight"],
        )
    except (TypeError, ValueError) as exc:
        raise CoordinateGateError(f"invalid geometry in payload: {exc}") from None


def _countdown(seconds: float, message: str) -> None:
    print(message)
    remaining = seconds
    step = 0.5
    while remaining > 0:
        print(f"  arming in {remaining:.1f}s...")
        time.sleep(min(step, remaining))
        remaining -= step


def run(source: str, countdown_s: float = 5.0) -> None:
    payload = load_payload(source)
    geometry = geometry_from_payload(payload)
    cx, cy = payload["cx"], payload["cy"]

    _countdown(countdown_s, "Switch to the diep.io browser tab now.")
    target = arm_foreground_window()

    try:
        client_origin = client_area_origin_on_screen(target)
    except Exception as exc:  # noqa: BLE001 -- report and fail closed, never guess
        raise CoordinateGateError(f"could not read target window geometry: {exc}") from None

    screen_x, screen_y = browser_point_to_controller_point(cx, cy, geometry, client_origin)
    print(f"[gate] target window: {target.title_at_arm!r} (hwnd={target.hwnd})")
    print(f"[gate] client-area origin (physical px): {client_origin}")
    print(f"[gate] Oracle canvas point: ({cx}, {cy})")
    print(f"[gate] predicted screen point: ({screen_x}, {screen_y})")

    controller = Controller()
    try:
        controller.arm(target)
        print(f"[gate] armed on: {target.title_at_arm!r}")
        controller.move_mouse(screen_x, screen_y)
        print(
            "[gate] cursor moved to the predicted point (no click sent). "
            "Visually compare the real cursor against the marker dropped "
            "by deepEyeOhProbe.markOracleShapes() on the same shape."
        )
    except ControlNotSafeError as exc:
        print(f"[gate] move refused by the safety gate: {exc}")
    finally:
        controller.shutdown()
