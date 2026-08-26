"""browser-overlay-control-v0: minimal parsing/dispatch for user-typed text
commands relayed from deep.eye.oh.ext's in-page overlay over the existing
WebSocket bridge (see browser_bridge.py). Mirrors browser_game_state.py's
parse/browser_policy.py's decide split: parsing is fail-closed and knows
nothing about dispatch semantics; dispatch is a pure function with no
Controller/bridge access of its own -- see browser_farming.py for how its
`effect` is applied.

v0 intentionally implements exactly one real capability (pause/resume,
built entirely from Controller.release_all() + a loop-level flag at the
call site -- see browser_farming.py) -- every other verb is parsed and
reported "unsupported", never guessed into new game policy. The overlay is
ears/mouth/dashboard for the bot, not part of its autonomous brain: this
module must never grow ad hoc behavior just to make a demo command "work".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Status = Literal["ok", "unsupported", "rejected"]
Effect = Literal["pause", "resume"] | None


class InvalidOverlayCommandError(ValueError):
    """A raw overlay_command bridge message fails the data contract.
    Callers must treat this as 'no usable command this message', same
    fail-closed discipline as browser_game_state.InvalidSnapshotError."""


@dataclass(frozen=True)
class OverlayCommand:
    text: str
    received_at: float


@dataclass(frozen=True)
class CommandResult:
    status: Status
    message: str
    effect: Effect = None


_SUPPORTED_EFFECTS: dict[str, Effect] = {"pause": "pause", "resume": "resume"}
_SUPPORTED_MESSAGES: dict[str, str] = {"pause": "bot paused", "resume": "bot resumed"}


def parse_overlay_command(raw: object, *, received_at: float) -> OverlayCommand:
    """Parse one raw {'type': 'overlay_command', ..., 'text': str} bridge
    message (see extension/background/bridge.js's buildCommandMessage).
    Fail-closed like parse_bridge_message: any structural problem raises
    InvalidOverlayCommandError rather than guessing or defaulting."""
    if not isinstance(raw, dict):
        raise InvalidOverlayCommandError(f"overlay command message: expected an object, got {type(raw).__name__}")
    if raw.get("type") != "overlay_command":
        raise InvalidOverlayCommandError(f"unexpected message type: {raw.get('type')!r}")
    text = raw.get("text")
    if not isinstance(text, str):
        raise InvalidOverlayCommandError(f"overlay command message: field 'text' must be a string, got {type(text).__name__}")
    return OverlayCommand(text=text, received_at=received_at)


def dispatch_command(command: OverlayCommand) -> CommandResult:
    """Pure -- never touches Controller/bridge, so it's trivially testable
    and cannot itself cause an unsafe action. Recognizes only pause/resume
    (case-insensitive, no arguments) as a real capability; every other verb
    -- including the mission's own example commands (mode, follow, tank
    target) -- is reported unsupported rather than silently implemented as
    new game policy."""
    stripped = command.text.strip()
    if not stripped:
        return CommandResult(status="rejected", message="empty command")

    tokens = stripped.split()
    verb = tokens[0].lower()

    if verb in _SUPPORTED_EFFECTS:
        if len(tokens) > 1:
            return CommandResult(status="unsupported", message=f"{verb!r} takes no arguments (got {stripped!r})")
        return CommandResult(status="ok", message=_SUPPORTED_MESSAGES[verb], effect=_SUPPORTED_EFFECTS[verb])

    return CommandResult(status="unsupported", message=f"unsupported command: {verb!r}")
