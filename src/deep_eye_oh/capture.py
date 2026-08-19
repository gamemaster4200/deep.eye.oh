"""Live screen capture, backed by PIL.ImageGrab (Windows GDI).

This is the only module that imports PIL.ImageGrab. A future backend
(mss, dxcam, a DirectX-based capturer) can replace ScreenCapture's
implementation without touching replay.py or cli.py, as long as it keeps
grab_one/grab_stream's signatures.
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Iterator

import numpy as np
from PIL import ImageGrab

from deep_eye_oh.observation import Observation, Viewport


class ScreenCapture:
    """Captures Observations from an explicitly-specified desktop region."""

    def __init__(self, viewport: Viewport) -> None:
        self.viewport = viewport

    def grab_one(self, frame_index: int) -> Observation:
        v = self.viewport
        bbox = (v.left, v.top, v.left + v.width, v.top + v.height)
        image = ImageGrab.grab(bbox=bbox, all_screens=True)
        timestamp = time.time()
        monotonic = time.perf_counter()

        if image.size != (v.width, v.height):
            raise RuntimeError(
                f"captured image size {image.size} does not match requested "
                f"viewport size {(v.width, v.height)} — likely a Windows "
                f"DPI/monitor-scaling mismatch"
            )

        frame = np.array(image.convert("RGB"), dtype=np.uint8)
        return Observation(
            frame=frame,
            timestamp=timestamp,
            monotonic=monotonic,
            frame_index=frame_index,
            viewport=v,
        )

    def grab_stream(
        self,
        *,
        duration_s: float | None = None,
        max_frames: int | None = None,
    ) -> Iterator[Observation]:
        """Yield Observations sequentially, no threading, no fps limiting.

        Stops when either bound is hit, whichever comes first. At least one
        of duration_s/max_frames must be set.
        """
        if duration_s is None and max_frames is None:
            raise ValueError("grab_stream requires duration_s and/or max_frames")

        start = time.perf_counter()
        for frame_index in itertools.count():
            if max_frames is not None and frame_index >= max_frames:
                return
            if duration_s is not None and time.perf_counter() - start >= duration_s:
                return
            yield self.grab_one(frame_index)
