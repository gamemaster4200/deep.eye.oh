"""Exercises cli.py's replay path against a fixture replay written directly
via ReplayWriter — no real capture, no display dependency. The capture
subcommand is deliberately left to the manual Windows E2E smoke run since
it requires a real screen."""

import json

import numpy as np
import pytest

from deep_eye_oh import __version__, cli
from deep_eye_oh.observation import Observation, Viewport
from deep_eye_oh.replay import ReplayWriter


def _write_fixture_replay(path, viewport, frame_count):
    with ReplayWriter(path, viewport) as writer:
        for i in range(frame_count):
            frame = np.full((viewport.height, viewport.width, 3), i % 256, dtype=np.uint8)
            obs = Observation(
                frame=frame,
                timestamp=100.0 + i,
                monotonic=10.0 + i,
                frame_index=i,
                viewport=viewport,
            )
            writer.write(obs)


def test_cli_replay_prints_stats(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=3)

    cli.main(["replay", "--in", str(replay_dir)])

    out = capsys.readouterr().out
    assert "frames replayed: 3 (manifest frame_count: 3)" in out
    assert "WARNING" not in out


def test_cli_replay_warns_on_mismatched_frame_count(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=3)

    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["frame_count"] = 99
    manifest_path.write_text(json.dumps(manifest))

    cli.main(["replay", "--in", str(replay_dir)])

    out = capsys.readouterr().out
    assert "WARNING" in out


def test_cli_replay_inspect_prints_frame_fields(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=3)

    cli.main(["replay-inspect", "--in", str(replay_dir), "--frame", "1"])

    out = capsys.readouterr().out
    assert "frame_index: 1" in out
    assert "shape=(4, 4, 3)" in out


def test_cli_replay_inspect_can_save_png(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=2)
    out_png = tmp_path / "frame.png"

    cli.main(["replay-inspect", "--in", str(replay_dir), "--frame", "0", "--save", str(out_png)])

    assert out_png.exists()
    out = capsys.readouterr().out
    assert "saved to" in out


def test_cli_replay_inspect_out_of_range_frame(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=2)

    cli.main(["replay-inspect", "--in", str(replay_dir), "--frame", "99"])

    out = capsys.readouterr().out
    assert "ERROR" in out
    assert "out of range" in out


def test_cli_replay_timing_prints_stats(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=5)

    cli.main(["replay-timing", "--in", str(replay_dir)])

    out = capsys.readouterr().out
    assert "frames read: 5" in out
    assert "delta t (s):" in out
    assert "implied fps" in out


def test_cli_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["--version"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"deep-eye-oh {__version__}"


def test_cli_doctor_exits_with_run_doctor_return_code(monkeypatch):
    import deep_eye_oh.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: 0)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["doctor"])
    assert exc_info.value.code == 0

    monkeypatch.setattr(doctor_mod, "run_doctor", lambda: 1)
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["doctor"])
    assert exc_info.value.code == 1


def test_cli_replay_timing_too_few_frames(tmp_path, capsys):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    _write_fixture_replay(replay_dir, vp, frame_count=1)

    cli.main(["replay-timing", "--in", str(replay_dir)])

    out = capsys.readouterr().out
    assert "not enough frames" in out
