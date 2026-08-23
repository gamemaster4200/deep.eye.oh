"""BrowserPolicy v0: a deliberately simple farming policy over
BrowserGameState -- see nearest neutral shape, aim at it, shoot, move
toward it, re-evaluate. No planning, no RL, no combat, no dodge (see
CLAUDE.md's browser-informed-farming-v0 non-goals).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from deep_eye_oh.browser_game_state import BrowserGameState, BrowserShape

# Lower cost wins. A small per-class weight lets a farther-but-more-valuable
# shape occasionally beat a nearer, low-value one, without building a real
# utility-AI system -- just distance with a light multiplier.
CLASS_VALUE_WEIGHT = {"square": 1.0, "triangle": 1.2, "pentagon": 1.6}

DEFAULT_MOVEMENT_DEADZONE_PX = 4.0


@dataclass(frozen=True)
class BrowserAction:
    """aim_x/aim_y are in the same Oracle canvas-backing pixel space as
    BrowserShape.cx/cy -- browser_farming.py is responsible for converting
    them to a physical screen point before calling Controller. move_keys
    is the subset of {"w","a","s","d"} to hold. has_target is False only
    for the explicit NOOP -- the loop must not aim/move/shoot then."""

    aim_x: float | None
    aim_y: float | None
    move_keys: frozenset
    shoot: bool

    @property
    def has_target(self) -> bool:
        return self.aim_x is not None and self.aim_y is not None


NOOP = BrowserAction(aim_x=None, aim_y=None, move_keys=frozenset(), shoot=False)


def _target_cost(shape: BrowserShape, origin: tuple[float, float]) -> float:
    distance = math.hypot(shape.cx - origin[0], shape.cy - origin[1])
    return distance * CLASS_VALUE_WEIGHT.get(shape.shape_class, 1.0)


def select_target(state: BrowserGameState, origin: tuple[float, float]) -> BrowserShape | None:
    """Picks the lowest-cost visible shape, or None if there are none.
    `origin` is the reference point (self position) in the same
    coordinate space as shape.cx/cy."""
    if not state.shapes:
        return None
    return min(state.shapes, key=lambda shape: _target_cost(shape, origin))


def _movement_keys(dx: float, dy: float, *, deadzone: float) -> frozenset:
    keys = set()
    if dx > deadzone:
        keys.add("d")
    elif dx < -deadzone:
        keys.add("a")
    if dy > deadzone:
        keys.add("s")  # canvas y+ is down on screen
    elif dy < -deadzone:
        keys.add("w")
    return frozenset(keys)


class BrowserPolicy:
    """v0: aim at + shoot + move toward the lowest-cost visible neutral
    shape; re-evaluated on every decide() call, no internal state."""

    def __init__(self, movement_deadzone_px: float = DEFAULT_MOVEMENT_DEADZONE_PX) -> None:
        self._deadzone = movement_deadzone_px

    def decide(self, state: BrowserGameState, origin: tuple[float, float]) -> BrowserAction:
        target = select_target(state, origin)
        if target is None:
            return NOOP
        dx = target.cx - origin[0]
        dy = target.cy - origin[1]
        return BrowserAction(
            aim_x=target.cx,
            aim_y=target.cy,
            move_keys=_movement_keys(dx, dy, deadzone=self._deadzone),
            shoot=True,
        )
