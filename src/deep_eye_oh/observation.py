"""The Observation contract: raw sensor data shared by live capture and replay.

Downstream code (eventually StateEstimator) must receive the same
Observation type regardless of whether it came from a live screen capture
or from reading a stored replay back.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Viewport:
    """A captured desktop region, in physical screen pixels."""

    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True, eq=False)
class Observation:
    """A single captured frame.

    Contract, enforced in __post_init__:
    - frame is RGB, dtype uint8, shape (viewport.height, viewport.width, 3).
    - timestamp is time.time() (wall-clock epoch seconds), for correlating
      an Observation with anything outside this process/session.
    - monotonic is time.perf_counter() (arbitrary-origin, monotonic
      seconds), the authoritative basis for frame-to-frame Δt — only
      differences between monotonic values from the same capture session
      are meaningful, but those differences stay valid after a replay
      round-trip, even read back in a different process.
    """

    frame: np.ndarray
    timestamp: float
    monotonic: float
    frame_index: int
    viewport: Viewport

    def __post_init__(self) -> None:
        expected_shape = (self.viewport.height, self.viewport.width, 3)
        if self.frame.dtype != np.uint8:
            raise ValueError(f"Observation.frame must be uint8, got {self.frame.dtype}")
        if self.frame.shape != expected_shape:
            raise ValueError(
                f"Observation.frame shape {self.frame.shape} does not match "
                f"viewport-derived shape {expected_shape}"
            )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Observation):
            return NotImplemented
        return (
            self.timestamp == other.timestamp
            and self.monotonic == other.monotonic
            and self.frame_index == other.frame_index
            and self.viewport == other.viewport
            and np.array_equal(self.frame, other.frame)
        )

    def __repr__(self) -> str:
        return (
            f"Observation(frame_index={self.frame_index}, "
            f"timestamp={self.timestamp!r}, monotonic={self.monotonic!r}, "
            f"viewport={self.viewport!r}, "
            f"frame=<ndarray shape={self.frame.shape} dtype={self.frame.dtype}>)"
        )
