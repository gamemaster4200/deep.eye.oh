"""Exercises browser_lifecycle.py: config load/save/defaults/validation
(atomic writes against a real tmp_path-backed LOCALAPPDATA, mirroring
test_browser_runtime.py's own env-var isolation pattern), lifecycle
message parsing (valid/invalid, fail-closed on unknown state), and the
bridge_hello/lifecycle_config wire-message helpers -- no real WebSocket,
no real bridge, no real browser."""

from __future__ import annotations

import json

import pytest

from deep_eye_oh import browser_lifecycle as bl


# ---------------------------------------------------------------------------
# BrowserFarmConfig / validate_config
# ---------------------------------------------------------------------------


def test_default_config_is_the_documented_zero_config_default():
    config = bl.BrowserFarmConfig()
    assert config.player_name == "deep.eye.oh"
    assert config.game_mode == "ffa"


def test_validate_config_accepts_a_known_mode():
    config = bl.validate_config("foo", "ffa")
    assert config == bl.BrowserFarmConfig(player_name="foo", game_mode="ffa")


def test_validate_config_rejects_unknown_mode():
    with pytest.raises(bl.InvalidConfigError, match="game_mode"):
        bl.validate_config("foo", "not_a_real_mode")


def test_validate_config_rejects_empty_name():
    with pytest.raises(bl.InvalidConfigError, match="player_name"):
        bl.validate_config("", "ffa")


def test_validate_config_rejects_name_over_max_length():
    with pytest.raises(bl.InvalidConfigError, match="player_name"):
        bl.validate_config("x" * (bl.MAX_PLAYER_NAME_LENGTH + 1), "ffa")


def test_validate_config_accepts_name_at_max_length():
    name = "x" * bl.MAX_PLAYER_NAME_LENGTH
    config = bl.validate_config(name, "ffa")
    assert config.player_name == name


def test_validate_config_rejects_non_string_fields():
    with pytest.raises(bl.InvalidConfigError):
        bl.validate_config(123, "ffa")
    with pytest.raises(bl.InvalidConfigError):
        bl.validate_config("foo", 123)


# ---------------------------------------------------------------------------
# config_path / load_config / save_config
# ---------------------------------------------------------------------------


def test_config_path_is_under_app_data_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert bl.config_path() == tmp_path / "deep-eye-oh" / "config.json"


def test_load_config_returns_defaults_when_no_file_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert bl.load_config() == bl.BrowserFarmConfig()


def test_save_then_load_config_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    original = bl.BrowserFarmConfig(player_name="my.bot", game_mode="teams")

    bl.save_config(original)
    loaded = bl.load_config()

    assert loaded == original


def test_save_config_writes_atomically_no_leftover_tmp_file(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    bl.save_config(bl.BrowserFarmConfig(player_name="foo", game_mode="ffa"))

    config_dir = bl.config_path().parent
    leftovers = [p for p in config_dir.iterdir() if p.name != "config.json"]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_save_config_rejects_invalid_config_before_writing(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Bypass the frozen dataclass's normal construction path (validate_config)
    # to simulate a caller handing save_config an already-invalid instance --
    # save_config must still refuse to write it (defense in depth).
    bad = bl.BrowserFarmConfig(player_name="", game_mode="ffa")

    with pytest.raises(bl.InvalidConfigError):
        bl.save_config(bad)
    assert not bl.config_path().exists()


def test_load_config_raises_clearly_on_corrupt_json(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = bl.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(bl.InvalidConfigError, match="JSON"):
        bl.load_config()


def test_load_config_raises_clearly_on_invalid_stored_values(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = bl.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"player_name": "ok", "game_mode": "not_a_mode"}), encoding="utf-8")

    with pytest.raises(bl.InvalidConfigError, match="game_mode"):
        bl.load_config()


def test_load_config_raises_on_non_object_json(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = bl.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    with pytest.raises(bl.InvalidConfigError, match="object"):
        bl.load_config()


def test_load_config_fills_missing_fields_with_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = bl.config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"player_name": "custom"}), encoding="utf-8")

    config = bl.load_config()
    assert config.player_name == "custom"
    assert config.game_mode == "ffa"


# ---------------------------------------------------------------------------
# parse_lifecycle_message: valid cases
# ---------------------------------------------------------------------------


def _raw_lifecycle(state="LOBBY", reason="home_screen_ready", selected_mode="ffa", **overrides):
    message = {
        "type": "lifecycle_snapshot",
        "tabId": 1,
        "observedAtMs": 123.0,
        "snapshot": {"state": state, "reason": reason, "selectedMode": selected_mode},
    }
    message.update(overrides)
    return message


@pytest.mark.parametrize("state", [s.value for s in bl.BrowserLifecycleState])
def test_parse_lifecycle_message_accepts_every_known_state(state):
    snapshot = bl.parse_lifecycle_message(_raw_lifecycle(state=state), received_at=5.0)
    assert snapshot.state is bl.BrowserLifecycleState(state)
    assert snapshot.reason == "home_screen_ready"
    assert snapshot.selected_mode == "ffa"
    assert snapshot.received_at == 5.0


def test_parse_lifecycle_message_allows_null_selected_mode():
    raw = _raw_lifecycle(selected_mode=None)
    snapshot = bl.parse_lifecycle_message(raw, received_at=1.0)
    assert snapshot.selected_mode is None


def test_parse_lifecycle_message_allows_missing_reason_defaults_empty():
    raw = _raw_lifecycle()
    del raw["snapshot"]["reason"]
    snapshot = bl.parse_lifecycle_message(raw, received_at=1.0)
    assert snapshot.reason == ""


# ---------------------------------------------------------------------------
# parse_lifecycle_message: invalid/fail-closed cases
# ---------------------------------------------------------------------------


def test_parse_lifecycle_message_rejects_wrong_type():
    with pytest.raises(bl.InvalidLifecycleMessageError, match="type"):
        bl.parse_lifecycle_message({"type": "oracle_snapshot", "snapshot": {}}, received_at=1.0)


def test_parse_lifecycle_message_rejects_unknown_state():
    with pytest.raises(bl.InvalidLifecycleMessageError, match="unknown"):
        bl.parse_lifecycle_message(_raw_lifecycle(state="TOTALLY_MADE_UP"), received_at=1.0)


def test_parse_lifecycle_message_rejects_missing_state():
    raw = _raw_lifecycle()
    del raw["snapshot"]["state"]
    with pytest.raises(bl.InvalidLifecycleMessageError):
        bl.parse_lifecycle_message(raw, received_at=1.0)


def test_parse_lifecycle_message_rejects_non_dict_snapshot():
    raw = _raw_lifecycle()
    raw["snapshot"] = "not a dict"
    with pytest.raises(bl.InvalidLifecycleMessageError):
        bl.parse_lifecycle_message(raw, received_at=1.0)


def test_parse_lifecycle_message_rejects_non_string_reason():
    raw = _raw_lifecycle()
    raw["snapshot"]["reason"] = 42
    with pytest.raises(bl.InvalidLifecycleMessageError):
        bl.parse_lifecycle_message(raw, received_at=1.0)


def test_parse_lifecycle_message_rejects_non_string_selected_mode():
    raw = _raw_lifecycle()
    raw["snapshot"]["selectedMode"] = 42
    with pytest.raises(bl.InvalidLifecycleMessageError):
        bl.parse_lifecycle_message(raw, received_at=1.0)


def test_parse_lifecycle_message_rejects_non_dict_message():
    with pytest.raises(bl.InvalidLifecycleMessageError):
        bl.parse_lifecycle_message("not a dict", received_at=1.0)


# ---------------------------------------------------------------------------
# bridge_hello validation
# ---------------------------------------------------------------------------


def test_validate_bridge_hello_accepts_well_formed_hello():
    bl.validate_bridge_hello(
        {"type": "bridge_hello", "protocolVersion": 1, "capabilities": ["oracle_snapshot", "lifecycle_v0"]}
    )  # must not raise


def test_validate_bridge_hello_rejects_wrong_type():
    with pytest.raises(bl.InvalidBridgeHelloError):
        bl.validate_bridge_hello({"type": "not_hello", "protocolVersion": 1, "capabilities": []})


def test_validate_bridge_hello_rejects_wrong_protocol_version():
    with pytest.raises(bl.InvalidBridgeHelloError, match="protocolVersion"):
        bl.validate_bridge_hello({"type": "bridge_hello", "protocolVersion": 2, "capabilities": []})


def test_validate_bridge_hello_rejects_non_list_capabilities():
    with pytest.raises(bl.InvalidBridgeHelloError):
        bl.validate_bridge_hello({"type": "bridge_hello", "protocolVersion": 1, "capabilities": "oracle_snapshot"})


def test_validate_bridge_hello_rejects_non_string_capability_entries():
    with pytest.raises(bl.InvalidBridgeHelloError):
        bl.validate_bridge_hello({"type": "bridge_hello", "protocolVersion": 1, "capabilities": [1, 2]})


def test_validate_bridge_hello_rejects_non_dict():
    with pytest.raises(bl.InvalidBridgeHelloError):
        bl.validate_bridge_hello("not a dict")


# ---------------------------------------------------------------------------
# build_lifecycle_config_message: the one Python->extension message
# ---------------------------------------------------------------------------


def test_build_lifecycle_config_message_shape():
    config = bl.BrowserFarmConfig(player_name="deep.eye.oh", game_mode="ffa")
    message = bl.build_lifecycle_config_message(config)
    assert message == {"type": "lifecycle_config", "playerName": "deep.eye.oh", "gameMode": "ffa"}
    json.dumps(message)  # must be JSON-safe
