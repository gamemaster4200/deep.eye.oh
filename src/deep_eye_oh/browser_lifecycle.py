"""browser-lifecycle-v0: the Python side of diep.io's own pre-game/lobby/
death lifecycle -- a small, separate model from browser_game_state.py's
Oracle `BrowserGameState` (gameplay perception). This module never touches
pixels/OpenCV/Windows input; it only parses lifecycle telemetry forwarded
by deep.eye.oh.ext's `lifecycle.js` content script (see that repo) over
the existing bridge, and owns the one canonical `BrowserFarmConfig` (player
name + game mode) stored under the application data root.

Live DOM reconnaissance (real https://diep.io/, Chrome for Testing, raw
CDP -- see this slice's PR description) is what the classification states,
selector choices, and CAPTCHA-detection strategy below are grounded in --
none of it is guessed. In particular:

  * diep.io's own UI is built around a `#screen-holder` with exactly one
    `.screen.active` child at a time (`#loading-screen`, `#home-screen`,
    `#in-game-screen`, `#game-over-screen`, `#status-message-screen`) --
    lifecycle.js's classifier (mirrored here only in the wire-message
    contract, not duplicated) is keyed off that.
  * The actual CAPTCHA/anti-bot challenge is Cloudflare Turnstile (not
    Google reCAPTCHA -- an unrelated, always-invisible ad-network iframe
    is also present in the DOM and is not what gates play), triggered
    SERVER-side on a join attempt (a `needsChallenge` response), not
    always shown on page load. Its widget, once rendered, always injects
    an `iframe[src^="https://challenges.cloudflare.com/"]` -- a stable,
    external, non-spoofable signal.

`parse_lifecycle_message` is fail-closed exactly like
`browser_game_state.parse_bridge_message`: one bad/unknown field rejects
the whole message rather than guessing a default, and an unknown lifecycle
state string is never silently mapped to something safe-looking.
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass
from pathlib import Path

from deep_eye_oh import browser_runtime

# Confirmed by live reconnaissance against #gamemode-selector's real option
# list (data-value attributes): ffa, teams, 4teams, maze, event, sandbox.
# "ctf" also appears in the site's shared color-lookup table but was not
# observed as an actual selectable option in the live dropdown, so it is
# deliberately excluded here -- only modes seen as real, selectable UI are
# accepted.
VALID_GAME_MODES = frozenset({"ffa", "teams", "4teams", "maze", "event", "sandbox"})
DEFAULT_PLAYER_NAME = "deep.eye.oh"
DEFAULT_GAME_MODE = "ffa"
# #spawn-nickname's real maxlength attribute, observed live.
MAX_PLAYER_NAME_LENGTH = 15

BRIDGE_PROTOCOL_VERSION = 1


class InvalidConfigError(ValueError):
    """A stored or CLI-supplied browser-farm config value failed
    validation. Callers must fail clearly/loudly -- never silently fall
    back to a guessed value for a config file that exists but is
    invalid."""


class InvalidLifecycleMessageError(ValueError):
    """A raw lifecycle bridge message fails the data contract. Callers
    must treat this exactly like a malformed Oracle message: log and
    drop, never crash the bridge, never overwrite the last good
    lifecycle state."""


class InvalidBridgeHelloError(ValueError):
    """A raw `bridge_hello` message fails the data contract."""


# --- Config: the one canonical player-name/game-mode source ---------------


@dataclass(frozen=True)
class BrowserFarmConfig:
    player_name: str = DEFAULT_PLAYER_NAME
    game_mode: str = DEFAULT_GAME_MODE


def config_path() -> Path:
    """Under the same per-user application data root browser_runtime.py
    already uses for the Chrome cache/profile -- survives package
    upgrades (an installed-wheel reinstall never touches this
    directory), and is the ONLY place browser-farm config lives (the
    extension keeps no independent copy -- see this slice's PR
    description)."""
    return browser_runtime.app_data_root() / "config.json"


def validate_config(player_name: object, game_mode: object, *, context: str = "config") -> BrowserFarmConfig:
    """Fail-closed validation shared by load_config (stored file),
    save_config (defense in depth before writing), and the `configure`
    CLI (user-supplied overrides) -- one validation path, so a bad value
    can never reach either the config file or a live session."""
    if not isinstance(player_name, str) or not (1 <= len(player_name) <= MAX_PLAYER_NAME_LENGTH):
        raise InvalidConfigError(
            f"{context}: player_name must be a string of length 1-{MAX_PLAYER_NAME_LENGTH}, got {player_name!r}"
        )
    if not isinstance(game_mode, str) or game_mode not in VALID_GAME_MODES:
        raise InvalidConfigError(
            f"{context}: game_mode must be one of {sorted(VALID_GAME_MODES)}, got {game_mode!r}"
        )
    return BrowserFarmConfig(player_name=player_name, game_mode=game_mode)


def load_config() -> BrowserFarmConfig:
    """Returns defaults if no config file exists yet -- the zero-
    configuration demo (`browser-farm` with no prior `configure` call)
    must keep working. A file that exists but is unreadable/corrupt/
    invalid raises InvalidConfigError rather than silently falling back
    to defaults -- that would mask a real problem the operator needs to
    see."""
    path = config_path()
    if not path.is_file():
        return BrowserFarmConfig()
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidConfigError(f"{path}: could not read config: {exc}") from exc
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InvalidConfigError(f"{path}: config is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise InvalidConfigError(f"{path}: expected a JSON object, got {type(raw).__name__}")
    player_name = raw.get("player_name", DEFAULT_PLAYER_NAME)
    game_mode = raw.get("game_mode", DEFAULT_GAME_MODE)
    return validate_config(player_name, game_mode, context=str(path))


def save_config(config: BrowserFarmConfig) -> None:
    """Atomic write (temp file + os.replace, mirroring
    browser_runtime._download's tmp.replace(dest) pattern) so a crash or
    concurrent read mid-write never sees/leaves a corrupt config file."""
    validate_config(config.player_name, config.game_mode, context="save_config")  # defense in depth
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    payload = {"player_name": config.player_name, "game_mode": config.game_mode}
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- Lifecycle state/snapshot ----------------------------------------------


class BrowserLifecycleState(enum.Enum):
    UNKNOWN = "UNKNOWN"
    LOADING = "LOADING"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    LOBBY = "LOBBY"
    ENTERING_GAME = "ENTERING_GAME"
    PLAYING = "PLAYING"
    DEAD = "DEAD"


_KNOWN_LIFECYCLE_STATES = frozenset(s.value for s in BrowserLifecycleState)


@dataclass(frozen=True)
class BrowserLifecycleSnapshot:
    state: BrowserLifecycleState
    reason: str
    selected_mode: str | None
    received_at: float  # this process's time.monotonic() at receipt


def _require_dict(value: object, context: str, exc_cls: type[ValueError] = InvalidLifecycleMessageError) -> dict:
    if not isinstance(value, dict):
        raise exc_cls(f"{context}: expected an object, got {type(value).__name__}")
    return value


def parse_lifecycle_message(raw: object, *, received_at: float) -> BrowserLifecycleSnapshot:
    """Parse one raw `lifecycle_snapshot` bridge message (see
    deep_eye_oh_ext's background/bridge.js). Deliberately fails closed on
    an unknown `state` value rather than mapping it to UNKNOWN itself --
    an unrecognized state string most likely means lifecycle.js and this
    module have drifted out of sync, which is exactly the kind of thing
    that must be loud (dropped + logged upstream), not silently
    tolerated as if it were a legitimate observed UNKNOWN."""
    message = _require_dict(raw, "lifecycle message")
    if message.get("type") != "lifecycle_snapshot":
        raise InvalidLifecycleMessageError(f"unexpected message type: {message.get('type')!r}")
    snapshot = _require_dict(message.get("snapshot"), "lifecycle message")

    state_raw = snapshot.get("state")
    if state_raw not in _KNOWN_LIFECYCLE_STATES:
        raise InvalidLifecycleMessageError(f"unknown or missing lifecycle state: {state_raw!r}")
    state = BrowserLifecycleState(state_raw)

    reason = snapshot.get("reason", "")
    if not isinstance(reason, str):
        raise InvalidLifecycleMessageError("snapshot.reason must be a string")

    selected_mode = snapshot.get("selectedMode")
    if selected_mode is not None and not isinstance(selected_mode, str):
        raise InvalidLifecycleMessageError("snapshot.selectedMode must be a string or null")

    return BrowserLifecycleSnapshot(state=state, reason=reason, selected_mode=selected_mode, received_at=received_at)


# --- bridge_hello / lifecycle_config wire messages -------------------------


def validate_bridge_hello(raw: object) -> None:
    """Validates (raises InvalidBridgeHelloError, never returns a value)
    the one browser->Python handshake message this slice adds alongside
    `oracle_snapshot`/`lifecycle_snapshot`. Does not require a specific
    `capabilities` entry -- just that it is a list of strings -- so a
    future capability can be added to the extension without this
    validator needing to change in lockstep."""
    message = _require_dict(raw, "bridge_hello message", exc_cls=InvalidBridgeHelloError)
    if message.get("type") != "bridge_hello":
        raise InvalidBridgeHelloError(f"unexpected message type: {message.get('type')!r}")
    if message.get("protocolVersion") != BRIDGE_PROTOCOL_VERSION:
        raise InvalidBridgeHelloError(
            f"unsupported protocolVersion: {message.get('protocolVersion')!r} "
            f"(expected {BRIDGE_PROTOCOL_VERSION})"
        )
    capabilities = message.get("capabilities")
    if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
        raise InvalidBridgeHelloError("capabilities must be a list of strings")


def build_lifecycle_config_message(config: BrowserFarmConfig) -> dict:
    """The ONLY Python->extension authority this slice adds: validated
    player-name/game-mode configuration. No selector, JavaScript, shell
    command, URL, or generic action payload is ever sent -- see this
    slice's PR description / deep_eye_oh_ext's AGENTS.md."""
    return {"type": "lifecycle_config", "playerName": config.player_name, "gameMode": config.game_mode}
