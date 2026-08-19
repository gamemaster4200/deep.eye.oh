"""Raw Windows keyboard/mouse injection via ctypes + SendInput.

The only module in this project that touches SendInput/ctypes structs
directly. Everything above this module deals in logical names ("w", "a",
"left") and physical pixel coordinates, never scan codes, VK codes, or
ctypes structures.

Keyboard injection uses KEYEVENTF_SCANCODE with physical hardware scan
codes, not virtual-key codes translated through the active keyboard
layout: control semantics track physical key *position*
(browser KeyboardEvent.code), independent of the operator's layout.

Mouse movement is absolute, normalized against the full Windows virtual
desktop (not just the primary monitor), so it works correctly across
multi-monitor layouts including a monitor placed left of/above the primary
(negative virtual-desktop origin). This does not implement or test DPI
awareness: coordinates here are only guaranteed to agree with
PIL.ImageGrab-based capture coordinates (deep_eye_oh.capture) on a display
at 100% scaling, or if the process is explicitly made DPI-aware (it
currently is not) -- a known v0 limitation.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)

# --- ctypes SendInput ABI ---------------------------------------------

ULONG_PTR = ctypes.c_size_t

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUnion(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_u", _INPUTUnion),
    ]


_EXPECTED_INPUT_SIZE = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
assert ctypes.sizeof(INPUT) == _EXPECTED_INPUT_SIZE, (
    f"ctypes INPUT struct is {ctypes.sizeof(INPUT)} bytes, expected "
    f"{_EXPECTED_INPUT_SIZE} -- SendInput struct layout is wrong for this "
    f"platform (32/64-bit union alignment mismatch)"
)

_SendInput = _user32.SendInput
_SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
_SendInput.restype = wintypes.UINT


def _send_raw_input(inputs: list[INPUT]) -> None:
    """The single call point into SendInput -- tests monkeypatch this."""
    count = len(inputs)
    array = (INPUT * count)(*inputs)
    sent = _SendInput(count, array, ctypes.sizeof(INPUT))
    if sent != count:
        error = ctypes.get_last_error()
        raise OSError(f"SendInput sent {sent}/{count} events (GetLastError={error})")


# --- Keyboard: physical scan codes, not VK codes ------------------------

# Standard PC/AT set-1 make scan codes for a US keyboard. Deliberately a
# small, curated set (WASD + a handful more) rather than an exhaustive map.
_SCAN_CODES: dict[str, int] = {
    "w": 0x11,
    "a": 0x1E,
    "s": 0x1F,
    "d": 0x20,
    "q": 0x10,
    "e": 0x12,
    "r": 0x13,
    "space": 0x39,
    "shift": 0x2A,
    "ctrl": 0x1D,
    "escape": 0x01,
    "enter": 0x1C,
    "tab": 0x0F,
    "1": 0x02,
    "2": 0x03,
    "3": 0x04,
    "4": 0x05,
    "5": 0x06,
    "6": 0x07,
    "7": 0x08,
    "8": 0x09,
}


def _scan_code(name: str) -> int:
    try:
        return _SCAN_CODES[name]
    except KeyError:
        raise ValueError(f"no scan code mapped for key {name!r}") from None


def _key_input(scan: int, *, key_up: bool) -> INPUT:
    flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
    ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_KEYBOARD, ki=ki)


def send_key_down(name: str) -> None:
    _send_raw_input([_key_input(_scan_code(name), key_up=False)])


def send_key_up(name: str) -> None:
    _send_raw_input([_key_input(_scan_code(name), key_up=True)])


# --- Mouse: absolute, full-virtual-desktop-normalized -------------------


def normalize_to_virtual_desktop(
    x: int, y: int, vx: int, vy: int, vw: int, vh: int
) -> tuple[int, int]:
    """Map a physical virtual-desktop pixel to Windows' 0-65535 absolute
    mouse coordinate space, endpoint-exact: (vx, vy) -> (0, 0) and
    (vx + vw - 1, vy + vh - 1) -> (65535, 65535).
    """
    norm_x = round((x - vx) * 65535 / (vw - 1)) if vw > 1 else 0
    norm_y = round((y - vy) * 65535 / (vh - 1)) if vh > 1 else 0
    return norm_x, norm_y


def _virtual_desktop_metrics() -> tuple[int, int, int, int]:
    get_metric = _user32.GetSystemMetrics
    return (
        get_metric(SM_XVIRTUALSCREEN),
        get_metric(SM_YVIRTUALSCREEN),
        get_metric(SM_CXVIRTUALSCREEN),
        get_metric(SM_CYVIRTUALSCREEN),
    )


def send_mouse_move(x: int, y: int) -> None:
    vx, vy, vw, vh = _virtual_desktop_metrics()
    norm_x, norm_y = normalize_to_virtual_desktop(x, y, vx, vy, vw, vh)
    mi = MOUSEINPUT(
        dx=norm_x,
        dy=norm_y,
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=0,
    )
    _send_raw_input([INPUT(type=INPUT_MOUSE, mi=mi)])


_BUTTON_DOWN_FLAGS: dict[str, int] = {
    "left": MOUSEEVENTF_LEFTDOWN,
    "right": MOUSEEVENTF_RIGHTDOWN,
    "middle": MOUSEEVENTF_MIDDLEDOWN,
}
_BUTTON_UP_FLAGS: dict[str, int] = {
    "left": MOUSEEVENTF_LEFTUP,
    "right": MOUSEEVENTF_RIGHTUP,
    "middle": MOUSEEVENTF_MIDDLEUP,
}


def _mouse_button_input(flag: int) -> INPUT:
    mi = MOUSEINPUT(dx=0, dy=0, mouseData=0, dwFlags=flag, time=0, dwExtraInfo=0)
    return INPUT(type=INPUT_MOUSE, mi=mi)


def send_mouse_button_down(button: str) -> None:
    try:
        flag = _BUTTON_DOWN_FLAGS[button]
    except KeyError:
        raise ValueError(f"unknown mouse button {button!r}") from None
    _send_raw_input([_mouse_button_input(flag)])


def send_mouse_button_up(button: str) -> None:
    try:
        flag = _BUTTON_UP_FLAGS[button]
    except KeyError:
        raise ValueError(f"unknown mouse button {button!r}") from None
    _send_raw_input([_mouse_button_input(flag)])
