"""Tests for coordinate_gate.py's payload parsing/validation -- the parts
that don't touch Controller/window_focus/win32 at all. run()'s live-move
path is exercised manually (see coordinate-gate CLI docs), not here --
same reasoning as smoke_test.py being excluded from the automated suite."""

import json

import pytest

from deep_eye_oh.coordinate_gate import (
    CoordinateGateError,
    geometry_from_payload,
    load_payload,
)

_VALID_PAYLOAD = {
    "cx": 960,
    "cy": 540,
    "canvasWidth": 1920,
    "canvasHeight": 1080,
    "canvasRectLeft": 0,
    "canvasRectTop": 0,
    "canvasRectWidth": 1920,
    "canvasRectHeight": 1080,
    "devicePixelRatio": 1.0,
    "innerWidth": 1920,
    "innerHeight": 1080,
    "outerWidth": 1920,
    "outerHeight": 1080,
}


def test_load_payload_from_file(tmp_path):
    p = tmp_path / "geometry.json"
    p.write_text(json.dumps(_VALID_PAYLOAD), encoding="utf-8")
    payload = load_payload(str(p))
    assert payload == _VALID_PAYLOAD


def test_load_payload_from_stdin(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_VALID_PAYLOAD)))
    payload = load_payload("-")
    assert payload == _VALID_PAYLOAD


def test_load_payload_rejects_missing_file():
    with pytest.raises(CoordinateGateError, match="could not read"):
        load_payload("this_file_does_not_exist.json")


def test_load_payload_rejects_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(CoordinateGateError, match="invalid JSON"):
        load_payload(str(p))


def test_load_payload_rejects_missing_fields(tmp_path):
    incomplete = {"cx": 1, "cy": 2}
    p = tmp_path / "incomplete.json"
    p.write_text(json.dumps(incomplete), encoding="utf-8")
    with pytest.raises(CoordinateGateError, match="missing required field"):
        load_payload(str(p))


def test_geometry_from_payload_builds_browser_geometry():
    geometry = geometry_from_payload(_VALID_PAYLOAD)
    assert geometry.canvas_width == 1920
    assert geometry.device_pixel_ratio == 1.0


def test_geometry_from_payload_fails_closed_on_invalid_geometry():
    bad = dict(_VALID_PAYLOAD, canvasRectWidth=0)
    with pytest.raises(CoordinateGateError, match="invalid geometry"):
        geometry_from_payload(bad)
