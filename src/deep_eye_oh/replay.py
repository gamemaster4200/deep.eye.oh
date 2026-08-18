"""Replay storage: a directory-based recording of Observations.

Format:

    <replay_dir>/
        manifest.json    # {format_version, viewport, frame_count, created_at}
        frames.jsonl      # one line per frame: {frame_index, timestamp, monotonic, file}
        frames/000000.png, 000001.png, ...

frames.jsonl is appended and flushed on every write, so an interrupted
capture leaves already-written frames and jsonl lines intact on disk.
manifest.json is written last, on a clean close — a replay directory
without a completed manifest.json is not a valid ReplayReader input. v0
does not attempt automatic partial-replay recovery; the raw frames/jsonl
remain manually salvageable if that's ever needed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import numpy as np
from PIL import Image

from deep_eye_oh.observation import Observation, Viewport

FORMAT_VERSION = 1


class ReplayWriter:
    """Writes a sequence of Observations to a new replay directory."""

    def __init__(self, path: Path, viewport: Viewport) -> None:
        path = Path(path)
        if path.exists():
            raise FileExistsError(f"replay directory already exists: {path}")
        self.path = path
        self.viewport = viewport
        self._frames_dir = path / "frames"
        self._frames_dir.mkdir(parents=True)
        self._jsonl_file = (path / "frames.jsonl").open("w", encoding="utf-8")
        self._created_at = time.time()
        self._frame_count = 0
        self._last_frame_index: int | None = None
        self._closed = False

    def write(self, observation: Observation) -> None:
        if self._closed:
            raise RuntimeError("cannot write to a closed ReplayWriter")
        if observation.viewport != self.viewport:
            raise ValueError(
                f"observation viewport {observation.viewport} does not match "
                f"replay viewport {self.viewport}"
            )
        if (
            self._last_frame_index is not None
            and observation.frame_index <= self._last_frame_index
        ):
            raise ValueError(
                f"frame_index must strictly increase: got "
                f"{observation.frame_index} after {self._last_frame_index}"
            )

        relative_file = f"frames/{observation.frame_index:06d}.png"
        Image.fromarray(observation.frame).save(self.path / relative_file)

        record = {
            "frame_index": observation.frame_index,
            "timestamp": observation.timestamp,
            "monotonic": observation.monotonic,
            "file": relative_file,
        }
        self._jsonl_file.write(json.dumps(record) + "\n")
        self._jsonl_file.flush()

        self._last_frame_index = observation.frame_index
        self._frame_count += 1

    def close(self) -> None:
        if self._closed:
            return
        self._jsonl_file.close()
        manifest = {
            "format_version": FORMAT_VERSION,
            "viewport": asdict(self.viewport),
            "frame_count": self._frame_count,
            "created_at": self._created_at,
        }
        (self.path / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        self._closed = True

    def __enter__(self) -> "ReplayWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ReplayReader:
    """Reads a replay directory back as a sequence of Observations."""

    def __init__(self, path: Path) -> None:
        path = Path(path)
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            raise FileNotFoundError(
                f"no manifest.json in {path} — replay is missing or was not "
                f"closed cleanly (interrupted capture); partial-replay "
                f"recovery is not supported"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["format_version"] != FORMAT_VERSION:
            raise ValueError(
                f"unsupported replay format_version "
                f"{manifest['format_version']!r}, expected {FORMAT_VERSION}"
            )

        self.path = path
        self.manifest = manifest
        self.viewport = Viewport(**manifest["viewport"])
        self.frame_count = manifest["frame_count"]

        jsonl_path = path / "frames.jsonl"
        text = jsonl_path.read_text(encoding="utf-8") if jsonl_path.exists() else ""
        self._records = [json.loads(line) for line in text.splitlines() if line.strip()]

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[Observation]:
        for record in self._records:
            with Image.open(self.path / record["file"]) as image:
                frame = np.array(image.convert("RGB"), dtype=np.uint8)
            yield Observation(
                frame=frame,
                timestamp=record["timestamp"],
                monotonic=record["monotonic"],
                frame_index=record["frame_index"],
                viewport=self.viewport,
            )

    def __enter__(self) -> "ReplayReader":
        return self

    def __exit__(self, *exc: object) -> None:
        pass
