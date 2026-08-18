import json

import numpy as np
import pytest

from deep_eye_oh.observation import Observation, Viewport
from deep_eye_oh.replay import FORMAT_VERSION, ReplayReader, ReplayWriter


def _make_observation(viewport, frame_index, seed):
    rng = np.random.default_rng(seed)
    frame = rng.integers(0, 256, size=(viewport.height, viewport.width, 3), dtype=np.uint8)
    return Observation(
        frame=frame,
        timestamp=1000.0 + frame_index * 0.1,
        monotonic=50.0 + frame_index * 0.1,
        frame_index=frame_index,
        viewport=viewport,
    )


def test_replay_round_trip(tmp_path):
    vp = Viewport(left=1, top=2, width=8, height=8)
    replay_dir = tmp_path / "replay"
    observations = [_make_observation(vp, i, seed=i) for i in range(5)]

    with ReplayWriter(replay_dir, vp) as writer:
        for obs in observations:
            writer.write(obs)

    reader = ReplayReader(replay_dir)
    assert reader.viewport == vp
    assert reader.frame_count == 5
    assert len(reader) == 5

    replayed = list(reader)
    assert len(replayed) == 5
    for original, replayed_obs in zip(observations, replayed):
        assert replayed_obs.frame_index == original.frame_index
        assert replayed_obs.timestamp == original.timestamp
        assert replayed_obs.monotonic == original.monotonic
        assert replayed_obs.viewport == original.viewport
        assert np.array_equal(replayed_obs.frame, original.frame)
        assert replayed_obs == original


def test_replay_empty(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "empty_replay"

    with ReplayWriter(replay_dir, vp):
        pass

    reader = ReplayReader(replay_dir)
    assert reader.frame_count == 0
    assert list(reader) == []


def test_replay_writer_refuses_existing_directory(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    replay_dir.mkdir()
    with pytest.raises(FileExistsError):
        ReplayWriter(replay_dir, vp)


def test_replay_reader_rejects_bad_format_version(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    with ReplayWriter(replay_dir, vp):
        pass

    manifest_path = replay_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["format_version"] = FORMAT_VERSION + 1
    manifest_path.write_text(json.dumps(manifest))

    with pytest.raises(ValueError):
        ReplayReader(replay_dir)


def test_replay_reader_rejects_missing_manifest(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    with ReplayWriter(replay_dir, vp) as writer:
        writer.write(_make_observation(vp, 0, seed=1))

    (replay_dir / "manifest.json").unlink()

    with pytest.raises(FileNotFoundError):
        ReplayReader(replay_dir)


def test_replay_writer_rejects_viewport_mismatch(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    other_vp = Viewport(left=0, top=0, width=8, height=8)
    replay_dir = tmp_path / "replay"
    with ReplayWriter(replay_dir, vp) as writer:
        bad_obs = _make_observation(other_vp, 0, seed=1)
        with pytest.raises(ValueError):
            writer.write(bad_obs)


def test_replay_writer_rejects_non_increasing_frame_index(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"
    with ReplayWriter(replay_dir, vp) as writer:
        writer.write(_make_observation(vp, 0, seed=1))
        with pytest.raises(ValueError):
            writer.write(_make_observation(vp, 0, seed=2))


def test_replay_writer_clean_close_writes_manifest(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"

    with ReplayWriter(replay_dir, vp) as writer:
        writer.write(_make_observation(vp, 0, seed=1))

    assert (replay_dir / "manifest.json").exists()
    reader = ReplayReader(replay_dir)
    assert reader.frame_count == 1


def test_replay_writer_aborts_on_exception_no_manifest(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"

    class _SimulatedFailure(Exception):
        pass

    with pytest.raises(_SimulatedFailure):
        with ReplayWriter(replay_dir, vp) as writer:
            writer.write(_make_observation(vp, 0, seed=1))
            writer.write(_make_observation(vp, 1, seed=2))
            raise _SimulatedFailure("simulated capture failure")

    # manifest must not exist — the replay is not considered complete
    assert not (replay_dir / "manifest.json").exists()

    # already-written frames/jsonl remain physically salvageable
    assert (replay_dir / "frames" / "000000.png").exists()
    assert (replay_dir / "frames" / "000001.png").exists()
    jsonl_lines = (replay_dir / "frames.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(jsonl_lines) == 2

    # ReplayReader must refuse to treat it as a valid replay
    with pytest.raises(FileNotFoundError):
        ReplayReader(replay_dir)


def test_replay_writer_aborts_on_keyboard_interrupt(tmp_path):
    vp = Viewport(left=0, top=0, width=4, height=4)
    replay_dir = tmp_path / "replay"

    with pytest.raises(KeyboardInterrupt):
        with ReplayWriter(replay_dir, vp) as writer:
            writer.write(_make_observation(vp, 0, seed=1))
            raise KeyboardInterrupt

    assert not (replay_dir / "manifest.json").exists()
    assert (replay_dir / "frames" / "000000.png").exists()
    with pytest.raises(FileNotFoundError):
        ReplayReader(replay_dir)
