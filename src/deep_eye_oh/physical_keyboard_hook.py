"""browser-overlay-control-v0: Windows-side physical/synthetic keyboard
disambiguation for the in-page overlay's command input.

Why this module exists (see CLAUDE.md's browser-overlay-control-v0
section for the full story): a live spike showed that giving the
overlay's command input real DOM focus in the browser causes this
project's OWN Controller-driven SendInput keyboard traffic to be consumed
as literal text by that focused input instead of reaching the game --
SendInput-injected and physical hardware keyboard events are NOT
distinguishable at the DOM/JS layer (both are `isTrusted: true`). The
only place that distinction actually exists is here: a Windows low-level
keyboard hook (WH_KEYBOARD_LL) sees an LLKHF_INJECTED flag on every event,
set only for SendInput-originated input.

PhysicalKeyboardCapture, while active, suppresses ONLY the physical
(non-injected) key events it actually consumes for the overlay's own
text/editing/toggle input (see translate_key()/_is_consumed_key()) --
those never reach the browser or any other application, and each
consumed keydown is relayed, translated to a small KeyEvent, to a
caller-supplied callback (see browser_bridge.py's push_key_event,
forwarded to the overlay over the existing bridge so it can render typed
text itself, since its command input is not natively focused in this
mode). Every OTHER physical key event -- unsupported/unrelated keys,
and both configured panic-key VK codes unconditionally (see
_NEVER_SUPPRESS_VK) -- passes through untouched via CallNextHookEx, same
as an unsuppressed key always would; this hook must never make a panic
key, Alt+Tab, or any other OS shortcut silently stop working just
because the overlay has focus. SendInput-injected events (LLKHF_INJECTED)
are, likewise, always passed through completely untouched via
CallNextHookEx, so Controller's own keyboard input keeps reaching the
game exactly as if this hook did not exist -- the bot never
pauses/degrades while the overlay is open, satisfying the mission's
explicit invariant.

Deliberately a separate, narrowly-scoped module (parallel to
win32_input.py), not folded into Controller: Controller stays exactly as
it was before this slice. Active only while the overlay's command input
actually has focus (browser_bridge.py starts/stops it in direct response
to the overlay_focus bridge message) -- kept as narrow in time as
possible, since a WH_KEYBOARD_LL hook is a global, system-wide keyboard
interception while installed.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HHOOK = wintypes.HANDLE

WH_KEYBOARD_LL = 13

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012

_PHYSICAL_MESSAGES = frozenset({WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP})
_KEYDOWN_MESSAGES = frozenset({WM_KEYDOWN, WM_SYSKEYDOWN})

LLKHF_INJECTED = 0x00000010

VK_BACK = 0x08
VK_PAUSE = 0x13
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_UP = 0x26
VK_DOWN = 0x28
VK_F9 = 0x78
VK_OEM_MINUS = 0xBD  # '-' / '_'
VK_OEM_2 = 0xBF  # '/' / '?'
VK_OEM_3 = 0xC0  # '`' (toggle key) / '~'

# emergency_stop.py's EmergencyStop supports exactly two panic-key options
# (its own _VK_CODES = {"pause": 0x13, "f9": 0x78}) -- mirrored here BY
# VALUE, not by importing that module, so this stays the same independent,
# narrowly-scoped module the class docstring describes. Neither VK is
# actually present in _SIMPLE_KEYS/_CHAR_KEYS below (so _is_consumed_key
# already excludes them structurally) -- this frozenset is a second,
# explicit, future-proof guarantee: a physical panic key must NEVER be
# suppressed by this hook, even if _SIMPLE_KEYS/_CHAR_KEYS is ever edited
# to (accidentally) start covering one of these VK codes. Note
# EmergencyStop's own detection is GetAsyncKeyState polling, independent
# of this hook's message suppression either way -- this is defense in
# depth, not the only thing standing between a suppressed key and safety.
_NEVER_SUPPRESS_VK: frozenset[int] = frozenset({VK_PAUSE, VK_F9})


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


_LowLevelKeyboardProc = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

_SetWindowsHookExW = _user32.SetWindowsHookExW
_SetWindowsHookExW.argtypes = (ctypes.c_int, _LowLevelKeyboardProc, wintypes.HINSTANCE, wintypes.DWORD)
_SetWindowsHookExW.restype = HHOOK

_UnhookWindowsHookEx = _user32.UnhookWindowsHookEx
_UnhookWindowsHookEx.argtypes = (HHOOK,)
_UnhookWindowsHookEx.restype = wintypes.BOOL

_CallNextHookEx = _user32.CallNextHookEx
_CallNextHookEx.argtypes = (HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_CallNextHookEx.restype = LRESULT

_GetMessageW = _user32.GetMessageW
_GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_GetMessageW.restype = wintypes.BOOL

_PostThreadMessageW = _user32.PostThreadMessageW
_PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_PostThreadMessageW.restype = wintypes.BOOL

_GetAsyncKeyState = _user32.GetAsyncKeyState
_GetAsyncKeyState.argtypes = (ctypes.c_int,)
_GetAsyncKeyState.restype = wintypes.SHORT

_GetCurrentThreadId = _kernel32.GetCurrentThreadId
_GetCurrentThreadId.restype = wintypes.DWORD


# --- VK code -> overlay key event (pure, testable without any real hook) ---

KeyEventKind = str  # "char" | "backspace" | "enter" | "escape" | "up" | "down" | "tilde"


@dataclass(frozen=True)
class KeyEvent:
    kind: KeyEventKind
    value: str | None = None  # set only for kind == "char"


_SIMPLE_KEYS: dict[int, KeyEventKind] = {
    VK_BACK: "backspace",
    VK_RETURN: "enter",
    VK_ESCAPE: "escape",
    VK_UP: "up",
    VK_DOWN: "down",
}

# vk -> (unshifted char, shifted char). v0 deliberately supports only what
# the mission's own example commands need (lowercase letters, digits,
# space, '/', '-', plus '!' and '~' for the shell-escape/toggle prefixes)
# -- not a full keyboard-layout implementation.
_CHAR_KEYS: dict[int, tuple[str, str]] = {
    **{vk: (chr(vk).lower(), chr(vk)) for vk in range(0x41, 0x5B)},  # A-Z
    **{vk: (chr(vk), chr(vk)) for vk in range(0x30, 0x3A)},  # 0-9
    VK_SPACE: (" ", " "),
    VK_OEM_MINUS: ("-", "_"),
    VK_OEM_2: ("/", "?"),
}
_CHAR_KEYS[0x31] = ("1", "!")  # the one shifted digit-row symbol v0 needs


def translate_key(vk_code: int, *, shift_held: bool) -> KeyEvent | None:
    """Pure function: one physical VK code (only meaningful for a KEYDOWN)
    -> a small overlay key event, or None for anything v0 doesn't support
    (silently ignored -- not every physical keystroke needs to reach the
    overlay, e.g. bare modifier keys, function keys). VK_OEM_3 is special:
    unshifted is the toggle key itself (closes the overlay while this hook
    is active, since open->open backtick never reaches the browser's own
    listener in this mode), shifted types a literal '~'."""
    if vk_code == VK_OEM_3:
        return KeyEvent(kind="char", value="~") if shift_held else KeyEvent(kind="tilde")
    if vk_code in _SIMPLE_KEYS:
        return KeyEvent(kind=_SIMPLE_KEYS[vk_code])
    if vk_code in _CHAR_KEYS:
        unshifted, shifted = _CHAR_KEYS[vk_code]
        return KeyEvent(kind="char", value=shifted if shift_held else unshifted)
    return None


def _is_consumed_key(vk_code: int) -> bool:
    """True iff this VK code is one _hook_proc actually consumes for the
    overlay -- i.e. translate_key() would produce a KeyEvent for it.
    shift_held never changes whether translate_key returns a KeyEvent at
    all (only which one -- see its own doc comment), so this check is
    valid for BOTH a keydown and the matching keyup of the same physical
    key, without needing shift state either. _NEVER_SUPPRESS_VK is
    checked first and wins unconditionally -- see its own doc comment."""
    if vk_code in _NEVER_SUPPRESS_VK:
        return False
    return translate_key(vk_code, shift_held=False) is not None


class PhysicalKeyboardCapture:
    """Owns a WH_KEYBOARD_LL hook; installed/pumped on its own background
    thread only between start() and stop() (idempotent both ways). See
    module docstring for the safety contract."""

    def __init__(self, on_key_event: Callable[[KeyEvent], None]) -> None:
        self._on_key_event = on_key_event
        self._hook: int | None = None
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        # ctypes keeps no reference to a WINFUNCTYPE instance on its own --
        # this attribute is what keeps the callback alive for the hook's
        # lifetime; letting it get garbage-collected while installed would
        # crash the process on the next physical keystroke.
        self._proc = _LowLevelKeyboardProc(self._hook_proc)

    def _hook_proc(self, n_code: int, w_param: int, l_param: int) -> int:
        # Every Win32 hook proc must forward n_code < 0 untouched, no
        # exceptions -- see SetWindowsHookEx/WH_KEYBOARD_LL documentation.
        if n_code >= 0 and w_param in _PHYSICAL_MESSAGES:
            info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            # LLKHF_INJECTED (this project's own SendInput traffic) always
            # falls through to CallNextHookEx below, untouched -- see
            # module docstring. A physical key this hook does not actually
            # consume for the overlay (panic keys, unrelated OS shortcuts,
            # anything translate_key() doesn't map) must ALSO fall through
            # -- suppressing it here would silently break it system-wide
            # for as long as the overlay has focus, which is not this
            # hook's job. _is_consumed_key() answers the same way for a
            # key's KEYDOWN and its matching KEYUP (see its own doc
            # comment), so a consumed key's down/up pair is suppressed
            # consistently -- never one without the other.
            if not (info.flags & LLKHF_INJECTED) and _is_consumed_key(info.vkCode):
                if w_param in _KEYDOWN_MESSAGES:
                    shift_held = bool(_GetAsyncKeyState(VK_SHIFT) & 0x8000)
                    event = translate_key(info.vkCode, shift_held=shift_held)
                    if event is not None:
                        try:
                            self._on_key_event(event)
                        except Exception:  # noqa: BLE001 -- a callback bug must never wedge the hook
                            logger.exception("overlay key-event callback raised")
                # Suppress only THIS consumed key's own down AND up --
                # never reaches the browser/game/any other app.
                return 1
        return _CallNextHookEx(0, n_code, w_param, l_param)

    def _run(self) -> None:
        self._thread_id = _GetCurrentThreadId()
        self._hook = _SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            logger.warning("SetWindowsHookExW(WH_KEYBOARD_LL) failed (GetLastError=%s)", ctypes.get_last_error())
            self._ready.set()
            return
        self._ready.set()
        # WH_KEYBOARD_LL callbacks are delivered directly by the hook
        # mechanism to this thread; the message loop's only job is to
        # keep the thread alive and pumping until WM_QUIT (see stop()).
        msg = wintypes.MSG()
        while _GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        _UnhookWindowsHookEx(self._hook)
        self._hook = None

    def start(self) -> None:
        if self._thread is not None:
            return  # already active -- idempotent
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="PhysicalKeyboardCapture", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2.0)

    def stop(self, timeout: float = 2.0) -> None:
        if self._thread is None:
            return  # already inactive -- idempotent
        if self._thread_id is not None:
            _PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout=timeout)
        self._thread = None
        self._thread_id = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
