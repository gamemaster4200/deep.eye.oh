"""Tests for physical_keyboard_hook.py.

translate_key() is pure and fully tested directly. _hook_proc's
injected-vs-physical branching is tested by calling it directly with a
fabricated KBDLLHOOKSTRUCT (no real OS hook installed for these cases --
see win32_input.py's own _send_raw_input single-call-point precedent for
this project's testable-seam style). A real SetWindowsHookExW/
UnhookWindowsHookEx install/uninstall cycle is exercised once (this
project's existing tests already exercise other real Win32 mechanisms,
e.g. window_focus/emergency_stop), but never with a real or synthesized
keystroke flowing through it -- see the module docstring and the plan's
"Live spike" section for why real physical-vs-SendInput disambiguation is
inherently untestable outside a manual smoke session.
"""

import ctypes

import pytest

from deep_eye_oh import physical_keyboard_hook as pkh
from deep_eye_oh.physical_keyboard_hook import (
    LLKHF_INJECTED,
    VK_BACK,
    VK_DOWN,
    VK_ESCAPE,
    VK_F9,
    VK_OEM_2,
    VK_OEM_3,
    VK_PAUSE,
    VK_RETURN,
    VK_SPACE,
    VK_UP,
    WM_KEYDOWN,
    WM_KEYUP,
    KBDLLHOOKSTRUCT,
    KeyEvent,
    PhysicalKeyboardCapture,
    _is_consumed_key,
    translate_key,
)

# ---------------------------------------------------------------------------
# translate_key: pure VK-code -> KeyEvent translation
# ---------------------------------------------------------------------------


def test_translate_letter_unshifted_is_lowercase():
    assert translate_key(ord("W"), shift_held=False) == KeyEvent(kind="char", value="w")


def test_translate_letter_shifted_is_uppercase():
    assert translate_key(ord("W"), shift_held=True) == KeyEvent(kind="char", value="W")


def test_translate_digit_unshifted():
    assert translate_key(ord("1"), shift_held=False) == KeyEvent(kind="char", value="1")


def test_translate_digit_one_shifted_is_bang():
    assert translate_key(ord("1"), shift_held=True) == KeyEvent(kind="char", value="!")


def test_translate_space():
    assert translate_key(VK_SPACE, shift_held=False) == KeyEvent(kind="char", value=" ")


def test_translate_oem_minus_shift_is_underscore():
    assert translate_key(0xBD, shift_held=False) == KeyEvent(kind="char", value="-")
    assert translate_key(0xBD, shift_held=True) == KeyEvent(kind="char", value="_")


def test_translate_oem_2_is_slash_or_question_mark():
    assert translate_key(VK_OEM_2, shift_held=False) == KeyEvent(kind="char", value="/")
    assert translate_key(VK_OEM_2, shift_held=True) == KeyEvent(kind="char", value="?")


def test_translate_oem_3_unshifted_is_toggle_not_a_character():
    assert translate_key(VK_OEM_3, shift_held=False) == KeyEvent(kind="tilde")


def test_translate_oem_3_shifted_is_literal_tilde_character():
    assert translate_key(VK_OEM_3, shift_held=True) == KeyEvent(kind="char", value="~")


@pytest.mark.parametrize(
    "vk, expected_kind",
    [
        (VK_BACK, "backspace"),
        (VK_RETURN, "enter"),
        (VK_ESCAPE, "escape"),
        (VK_UP, "up"),
        (VK_DOWN, "down"),
    ],
)
def test_translate_control_keys(vk, expected_kind):
    assert translate_key(vk, shift_held=False) == KeyEvent(kind=expected_kind)


def test_translate_unsupported_vk_returns_none():
    assert translate_key(0x70, shift_held=False) is None  # VK_F1 -- not in v0's supported set


# ---------------------------------------------------------------------------
# _is_consumed_key: the suppression gate -- must exclude panic keys and
# anything translate_key() does not map, regardless of shift state
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vk", [VK_PAUSE, VK_F9])
def test_panic_key_vk_codes_are_never_consumed(vk):
    # Both belt (translate_key structurally never maps these VKs -- see
    # _SIMPLE_KEYS/_CHAR_KEYS) and suspenders (_NEVER_SUPPRESS_VK's
    # explicit, future-proof override) must agree.
    assert translate_key(vk, shift_held=False) is None
    assert translate_key(vk, shift_held=True) is None
    assert _is_consumed_key(vk) is False


def test_unsupported_vk_is_not_consumed():
    assert _is_consumed_key(0x70) is False  # VK_F1


@pytest.mark.parametrize("vk", [ord("W"), VK_BACK, VK_RETURN, VK_ESCAPE, VK_UP, VK_DOWN, VK_OEM_3])
def test_overlay_keys_are_consumed(vk):
    assert _is_consumed_key(vk) is True


# ---------------------------------------------------------------------------
# _hook_proc: injected (SendInput) vs physical disambiguation
# ---------------------------------------------------------------------------


def _kbdllhookstruct_lparam(vk_code, *, injected, flags_extra=0):
    flags = flags_extra | (LLKHF_INJECTED if injected else 0)
    info = KBDLLHOOKSTRUCT(vkCode=vk_code, scanCode=0, flags=flags, time=0, dwExtraInfo=0)
    return ctypes.addressof(info), info  # keep `info` alive for the call's duration


def test_hook_proc_passes_through_injected_events(monkeypatch):
    events = []
    capture = PhysicalKeyboardCapture(on_key_event=events.append)

    calls = []
    monkeypatch.setattr(pkh, "_CallNextHookEx", lambda *a: calls.append(a) or 42)

    address, info = _kbdllhookstruct_lparam(ord("W"), injected=True)
    result = capture._hook_proc(0, WM_KEYDOWN, address)

    assert result == 42, "an injected (bot SendInput) event must be forwarded via CallNextHookEx, never suppressed"
    assert len(calls) == 1
    assert events == [], "injected events must never be translated/relayed as overlay typing"


def test_hook_proc_suppresses_and_translates_physical_keydown(monkeypatch):
    events = []
    capture = PhysicalKeyboardCapture(on_key_event=events.append)
    monkeypatch.setattr(pkh, "_GetAsyncKeyState", lambda vk: 0)  # shift not held
    monkeypatch.setattr(pkh, "_CallNextHookEx", lambda *a: pytest.fail("physical events must not reach CallNextHookEx"))

    address, info = _kbdllhookstruct_lparam(ord("W"), injected=False)
    result = capture._hook_proc(0, WM_KEYDOWN, address)

    assert result == 1, "a physical event must be suppressed (nonzero return, no CallNextHookEx)"
    assert events == [KeyEvent(kind="char", value="w")]


def test_hook_proc_suppresses_physical_keyup_without_emitting_event(monkeypatch):
    events = []
    capture = PhysicalKeyboardCapture(on_key_event=events.append)
    monkeypatch.setattr(pkh, "_CallNextHookEx", lambda *a: pytest.fail("physical events must not reach CallNextHookEx"))

    address, info = _kbdllhookstruct_lparam(ord("W"), injected=False)
    result = capture._hook_proc(0, WM_KEYUP, address)

    assert result == 1
    assert events == [], "key-up is suppressed like key-down, but never itself produces an overlay event"


# ---------------------------------------------------------------------------
# Regression: a physical key this hook does not consume for the overlay
# (panic keys, other unrelated OS shortcuts) must never be suppressed --
# see _hook_proc's doc comment and _is_consumed_key.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vk", [VK_PAUSE, VK_F9])
@pytest.mark.parametrize("message", [WM_KEYDOWN, WM_KEYUP])
def test_hook_proc_passes_through_physical_panic_key(monkeypatch, vk, message):
    events = []
    capture = PhysicalKeyboardCapture(on_key_event=events.append)
    monkeypatch.setattr(pkh, "_GetAsyncKeyState", lambda vk: 0)

    calls = []
    monkeypatch.setattr(pkh, "_CallNextHookEx", lambda *a: calls.append(a) or 99)

    address, info = _kbdllhookstruct_lparam(vk, injected=False)
    result = capture._hook_proc(0, message, address)

    assert result == 99, "a physical panic key must always reach CallNextHookEx, overlay focus or not"
    assert len(calls) == 1
    assert events == [], "a panic key must never be translated/relayed as overlay typing"


@pytest.mark.parametrize("message", [WM_KEYDOWN, WM_KEYUP])
def test_hook_proc_passes_through_unsupported_physical_key(monkeypatch, message):
    events = []
    capture = PhysicalKeyboardCapture(on_key_event=events.append)
    monkeypatch.setattr(pkh, "_GetAsyncKeyState", lambda vk: 0)

    calls = []
    monkeypatch.setattr(pkh, "_CallNextHookEx", lambda *a: calls.append(a) or 7)

    address, info = _kbdllhookstruct_lparam(0x70, injected=False)  # VK_F1
    result = capture._hook_proc(0, message, address)

    assert result == 7, "an unsupported/unrelated physical key must reach CallNextHookEx, not be swallowed"
    assert len(calls) == 1
    assert events == []


def test_hook_proc_negative_ncode_always_forwards_untouched(monkeypatch):
    calls = []
    capture = PhysicalKeyboardCapture(on_key_event=lambda e: pytest.fail("must not be called for nCode < 0"))
    monkeypatch.setattr(pkh, "_CallNextHookEx", lambda *a: calls.append(a) or 7)

    address, info = _kbdllhookstruct_lparam(ord("W"), injected=False)
    result = capture._hook_proc(-1, WM_KEYDOWN, address)

    assert result == 7
    assert len(calls) == 1


def test_hook_proc_callback_exception_never_propagates(monkeypatch):
    def boom(event):
        raise RuntimeError("boom")

    capture = PhysicalKeyboardCapture(on_key_event=boom)
    monkeypatch.setattr(pkh, "_GetAsyncKeyState", lambda vk: 0)

    address, info = _kbdllhookstruct_lparam(ord("W"), injected=False)
    result = capture._hook_proc(0, WM_KEYDOWN, address)  # must not raise

    assert result == 1


# ---------------------------------------------------------------------------
# PhysicalKeyboardCapture: real start/stop lifecycle (no keystrokes involved)
# ---------------------------------------------------------------------------


def test_start_stop_is_idempotent_and_toggles_active():
    capture = PhysicalKeyboardCapture(on_key_event=lambda e: None)
    assert capture.active is False

    capture.start()
    try:
        assert capture.active is True
        capture.start()  # idempotent
        assert capture.active is True
    finally:
        capture.stop()

    assert capture.active is False
    capture.stop()  # idempotent
    assert capture.active is False
