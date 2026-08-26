"""Tests for overlay_command.py: fail-closed parsing and pure dispatch
semantics for the browser overlay's text commands."""

import pytest

from deep_eye_oh.overlay_command import (
    CommandResult,
    InvalidOverlayCommandError,
    OverlayCommand,
    dispatch_command,
    parse_overlay_command,
)

# ---------------------------------------------------------------------------
# parse_overlay_command: fail-closed
# ---------------------------------------------------------------------------


def test_parse_accepts_well_formed_message():
    command = parse_overlay_command({"type": "overlay_command", "text": "pause"}, received_at=1.0)
    assert command == OverlayCommand(text="pause", received_at=1.0)


@pytest.mark.parametrize(
    "raw",
    [
        "not a dict",
        None,
        123,
        [],
    ],
)
def test_parse_rejects_non_dict_message(raw):
    with pytest.raises(InvalidOverlayCommandError):
        parse_overlay_command(raw, received_at=1.0)


def test_parse_rejects_wrong_type_field():
    with pytest.raises(InvalidOverlayCommandError):
        parse_overlay_command({"type": "oracle_snapshot", "text": "pause"}, received_at=1.0)


def test_parse_rejects_missing_type_field():
    with pytest.raises(InvalidOverlayCommandError):
        parse_overlay_command({"text": "pause"}, received_at=1.0)


@pytest.mark.parametrize("bad_text", [None, 42, 3.5, True, [], {}])
def test_parse_rejects_non_string_text(bad_text):
    with pytest.raises(InvalidOverlayCommandError):
        parse_overlay_command({"type": "overlay_command", "text": bad_text}, received_at=1.0)


def test_parse_rejects_missing_text_field():
    with pytest.raises(InvalidOverlayCommandError):
        parse_overlay_command({"type": "overlay_command"}, received_at=1.0)


# ---------------------------------------------------------------------------
# dispatch_command: pure, no Controller/bridge access
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["pause", "PAUSE", "Pause", "  pause  "])
def test_dispatch_pause_is_supported_case_insensitive(text):
    result = dispatch_command(OverlayCommand(text=text, received_at=0.0))
    assert result == CommandResult(status="ok", message="bot paused", effect="pause")


@pytest.mark.parametrize("text", ["resume", "RESUME", "Resume"])
def test_dispatch_resume_is_supported_case_insensitive(text):
    result = dispatch_command(OverlayCommand(text=text, received_at=0.0))
    assert result == CommandResult(status="ok", message="bot resumed", effect="resume")


def test_dispatch_pause_with_arguments_is_unsupported():
    result = dispatch_command(OverlayCommand(text="pause now", received_at=0.0))
    assert result.status == "unsupported"
    assert result.effect is None


@pytest.mark.parametrize(
    "text",
    ["mode farm", "follow bot-17", "tank target overlord"],
)
def test_dispatch_unimplemented_mission_examples_are_unsupported_not_invented(text):
    result = dispatch_command(OverlayCommand(text=text, received_at=0.0))
    assert result.status == "unsupported"
    assert result.effect is None
    assert result.message  # a human-readable explanation is always present


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_dispatch_empty_command_is_rejected(text):
    result = dispatch_command(OverlayCommand(text=text, received_at=0.0))
    assert result.status == "rejected"
    assert result.effect is None
