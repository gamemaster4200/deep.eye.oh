"""Browser-informed GameState v0.

Parses a raw bridge message (forwarded by deep.eye.oh.ext's
extension/background/bridge.js over a local WebSocket -- see
browser_bridge.py) into a strict, minimal internal representation, and
computes the coordinate transform from Oracle canvas-backing pixel space
to physical screen pixel space (what Controller.move_mouse expects).

This module deliberately does NOT try to be forgiving of malformed input:
one bad field rejects the whole message (InvalidSnapshotError), rather
than silently dropping just that field or that one shape. Downstream
(browser_farming.py) treats a rejected message exactly like a missing one
-- no autonomous input this tick -- which is the fail-closed behavior
this project's safety requirements call for. This is a real-time control
input, not a messy external corpus to curate best-effort from.
"""

from __future__ import annotations

from dataclasses import dataclass

KNOWN_SHAPE_CLASSES = frozenset({"square", "triangle", "pentagon"})


class InvalidSnapshotError(ValueError):
    """A raw bridge message fails the data contract. Callers must treat
    this as 'no usable state this tick', never crash the control loop."""


@dataclass(frozen=True)
class BrowserShape:
    """One neutral shape, in Oracle canvas-backing pixel space (the same
    coordinate system for square/triangle/pentagon -- see
    deep.eye.oh.ext's oracle.js)."""

    shape_class: str  # "square" | "triangle" | "pentagon"
    cx: float
    cy: float
    radius: float
    timestamp_ms: float  # oracle's performance.now() at detection


@dataclass(frozen=True)
class BrowserCircle:
    """One generic filled-circle observation, in the same Oracle
    canvas-backing pixel space as BrowserShape -- see deep.eye.oh.ext's
    oracle.js circles()/CircleObservation. Deliberately carries no class,
    ownership, or entity identity: only what rendering actually shows.
    `color` is best-effort (the fill's raw fillStyle string, or None if it
    was not a form the Oracle recognized)."""

    cx: float
    cy: float
    radius: float
    color: str | None
    timestamp_ms: float  # oracle's performance.now() at detection


@dataclass(frozen=True)
class CanvasInfo:
    """Oracle canvas positioning metadata (deepEyeOracle.snapshot().canvas),
    used only for the browser->screen coordinate transform below.

    browser_chrome_width_css/browser_chrome_height_css (window.outerWidth -
    innerWidth / outerHeight - innerHeight, CSS px, from oracle.js's own
    browserChromeOffsetCss()) default to 0.0 for backward compatibility
    with an older Oracle build/stored fixture that doesn't report them --
    matching this module's existing pattern for `circles` (see
    parse_bridge_message below). 0.0 means "assume no offset" -- live-
    smoke-confirmed to be the WRONG assumption for a real running browser
    (see compute_screen_transform's own docstring), but it is the only
    sensible default for data that genuinely never captured this."""

    width: float
    height: float
    rect_left: float
    rect_top: float
    rect_width: float
    rect_height: float
    device_pixel_ratio: float
    browser_chrome_width_css: float = 0.0
    browser_chrome_height_css: float = 0.0


@dataclass(frozen=True)
class BrowserGameState:
    shapes: tuple[BrowserShape, ...]
    circles: tuple[BrowserCircle, ...]
    canvas: CanvasInfo | None
    polled_at_ms: float  # bridge's Date.now() when it polled the tab
    performance_now_ms: float | None  # oracle's performance.now() at read
    received_at: float  # this process's time.monotonic() at receipt


def _require_dict(value: object, context: str) -> dict:
    if not isinstance(value, dict):
        raise InvalidSnapshotError(f"{context}: expected an object, got {type(value).__name__}")
    return value


def _require_number(d: dict, key: str, context: str) -> float:
    if key not in d:
        raise InvalidSnapshotError(f"{context}: missing required field {key!r}")
    value = d[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidSnapshotError(f"{context}: field {key!r} must be a number, got {type(value).__name__}")
    return float(value)


def _parse_shape(raw: object, index: int) -> BrowserShape:
    context = f"snapshot.shapes[{index}]"
    shape = _require_dict(raw, context)
    shape_class = shape.get("class")
    if shape_class not in KNOWN_SHAPE_CLASSES:
        raise InvalidSnapshotError(f"{context}: unknown or missing shape class {shape_class!r}")
    return BrowserShape(
        shape_class=shape_class,
        cx=_require_number(shape, "cx", context),
        cy=_require_number(shape, "cy", context),
        radius=_require_number(shape, "radius", context),
        timestamp_ms=_require_number(shape, "timestamp", context),
    )


def _parse_circle(raw: object, index: int) -> BrowserCircle:
    context = f"snapshot.circles[{index}]"
    circle = _require_dict(raw, context)
    color = circle.get("color")
    if color is not None and not isinstance(color, str):
        raise InvalidSnapshotError(f"{context}: field 'color' must be a string or absent, got {type(color).__name__}")
    return BrowserCircle(
        cx=_require_number(circle, "cx", context),
        cy=_require_number(circle, "cy", context),
        radius=_require_number(circle, "radius", context),
        color=color,
        timestamp_ms=_require_number(circle, "timestamp", context),
    )


def _optional_number(d: dict, key: str, context: str) -> float:
    """Like _require_number, but 0.0 (never present in a well-formed
    message from a current Oracle build -- see browserChromeOffsetCss's
    own fallback -- but absent entirely from an OLDER build, which is a
    strictly-additive-capability gap, not a malformed message) when the
    key is simply missing. Present-but-wrong-type still rejects the whole
    message, exactly like every other field here."""
    if key not in d:
        return 0.0
    return _require_number(d, key, context)


def _parse_canvas(raw: object) -> CanvasInfo:
    canvas = _require_dict(raw, "snapshot.canvas")
    rect = _require_dict(canvas.get("rect"), "snapshot.canvas.rect")
    return CanvasInfo(
        width=_require_number(canvas, "width", "snapshot.canvas"),
        height=_require_number(canvas, "height", "snapshot.canvas"),
        rect_left=_require_number(rect, "left", "snapshot.canvas.rect"),
        rect_top=_require_number(rect, "top", "snapshot.canvas.rect"),
        rect_width=_require_number(rect, "width", "snapshot.canvas.rect"),
        rect_height=_require_number(rect, "height", "snapshot.canvas.rect"),
        device_pixel_ratio=_require_number(canvas, "devicePixelRatio", "snapshot.canvas"),
        browser_chrome_width_css=_optional_number(canvas, "browserChromeWidthCss", "snapshot.canvas"),
        browser_chrome_height_css=_optional_number(canvas, "browserChromeHeightCss", "snapshot.canvas"),
    )


def parse_bridge_message(raw: object, *, received_at: float) -> BrowserGameState:
    """Parse one raw bridge message (see extension/background/bridge.js's
    buildOutboundMessage: {type, tabId, polledAtMs, snapshot}). Raises
    InvalidSnapshotError with an explicit reason on any structural
    problem -- never silently defaults or guesses. `received_at` is this
    process's own time.monotonic() reading at receipt, used for
    staleness checks downstream (see browser_bridge.py)."""
    message = _require_dict(raw, "bridge message")
    if message.get("type") != "oracle_snapshot":
        raise InvalidSnapshotError(f"unexpected message type: {message.get('type')!r}")
    polled_at_ms = _require_number(message, "polledAtMs", "bridge message")
    snapshot = _require_dict(message.get("snapshot"), "bridge message")

    shapes_raw = snapshot.get("shapes")
    if not isinstance(shapes_raw, list):
        raise InvalidSnapshotError("snapshot.shapes must be a list")
    shapes = tuple(_parse_shape(entry, index) for index, entry in enumerate(shapes_raw))

    # Absent entirely (an older Oracle build with no circles() capability)
    # is treated as "no circles observed", not a malformed message -- this
    # is a strictly additive capability. Present-but-wrong-type, or any one
    # malformed entry, still rejects the whole message like every other
    # field here (see module docstring).
    circles: tuple[BrowserCircle, ...] = ()
    if "circles" in snapshot:
        circles_raw = snapshot.get("circles")
        if not isinstance(circles_raw, list):
            raise InvalidSnapshotError("snapshot.circles must be a list")
        circles = tuple(_parse_circle(entry, index) for index, entry in enumerate(circles_raw))

    canvas = _parse_canvas(snapshot["canvas"]) if "canvas" in snapshot else None

    performance_now_ms = None
    timestamps = snapshot.get("timestamps")
    if isinstance(timestamps, dict):
        value = timestamps.get("performanceNow")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            performance_now_ms = float(value)

    return BrowserGameState(
        shapes=shapes,
        circles=circles,
        canvas=canvas,
        polled_at_ms=polled_at_ms,
        performance_now_ms=performance_now_ms,
        received_at=received_at,
    )


# --- Coordinate calibration: Oracle canvas-backing pixels -> physical ------
# --- screen pixels (what Controller.move_mouse expects) -------------------


@dataclass(frozen=True)
class ScreenTransform:
    """(backing_x, backing_y) -> physical screen pixel (x, y), an affine
    map: screen = backing * scale + offset."""

    scale_x: float
    scale_y: float
    offset_x: float
    offset_y: float

    def apply(self, x: float, y: float) -> tuple[int, int]:
        return (
            round((x * self.scale_x) + self.offset_x),
            round((y * self.scale_y) + self.offset_y),
        )


def compute_screen_transform(
    canvas: CanvasInfo, client_rect: tuple[int, int, int, int]
) -> ScreenTransform | None:
    """Derive the Oracle canvas-backing-pixel -> physical-screen-pixel
    transform empirically from the target window's LIVE client rect
    (window_focus.client_rect_on_screen), rather than from operator-
    supplied calibration constants: neither the browser window's physical
    on-screen origin nor the effective OS/browser display-scaling factor
    need to be known in advance, because both are implicit in the ratio
    between the physical client rect (from win32gui, the same coordinate
    space win32_input.send_mouse_move ultimately targets) and the
    Oracle's own CSS-pixel canvas rect (getBoundingClientRect()).

    The win32 client rect covers the WHOLE OS window content area,
    including the browser's OWN chrome (tab strip, omnibox, and -- for
    Chrome for Testing specifically -- an "automated testing only"
    infobar), which sits ABOVE the actual page viewport the canvas
    renders into -- live-smoke-confirmed (~179 physical px gap on a real
    diep.io session) that treating the whole client rect as if the canvas
    filled it computes screen points landing inside the browser's own UI
    (confirmed live: the address bar) instead of the game. canvas.
    browser_chrome_width_css/height_css (window.outerWidth/outerHeight -
    innerWidth/innerHeight, from oracle.js) are subtracted -- converted to
    physical px via canvas.device_pixel_ratio, confirmed live to be the
    correct CSS-to-physical-screen-pixel ratio for a DPI-aware process,
    since backing-store width already equals client width exactly when
    there is no horizontal chrome offset -- before deriving scale/offset,
    so both describe the actual page viewport, not the whole OS window.
    Both default to 0.0 for an older Oracle build/stored fixture that
    never reported them, reproducing the old (now known-wrong) behavior
    for that data rather than guessing.

    Returns None (fail closed) for degenerate/zero-sized input -- now
    including a viewport that would work out to zero or negative size --
    rather than dividing by zero or guessing.
    """
    client_left, client_top, client_width, client_height = client_rect
    if (
        client_width <= 0 or client_height <= 0
        or canvas.width <= 0 or canvas.height <= 0
        or canvas.rect_width <= 0 or canvas.rect_height <= 0
        or canvas.device_pixel_ratio <= 0
    ):
        return None

    chrome_width_physical = canvas.browser_chrome_width_css * canvas.device_pixel_ratio
    chrome_height_physical = canvas.browser_chrome_height_css * canvas.device_pixel_ratio
    viewport_width = client_width - chrome_width_physical
    viewport_height = client_height - chrome_height_physical
    if viewport_width <= 0 or viewport_height <= 0:
        return None

    # Physical screen pixels per Oracle backing pixel, directly -- see
    # this function's docstring for the derivation.
    scale_x = viewport_width / canvas.width
    scale_y = viewport_height / canvas.height
    css_to_screen_x = viewport_width / canvas.rect_width
    css_to_screen_y = viewport_height / canvas.rect_height
    offset_x = client_left + chrome_width_physical + (canvas.rect_left * css_to_screen_x)
    offset_y = client_top + chrome_height_physical + (canvas.rect_top * css_to_screen_y)
    return ScreenTransform(scale_x=scale_x, scale_y=scale_y, offset_x=offset_x, offset_y=offset_y)


# --- Circle post-processing: colocated-render merging -----------------------
#
# Live evidence (projectile-speed-and-lead-v0's live smoke -- see its PR
# description) showed diep.io commonly renders one circular entity (at
# least tank bodies) as TWO separate arc()/fill() calls at the exact same
# center and timestamp: a slightly larger "border" circle plus a slightly
# smaller "fill" circle, in two different but close shades. Oracle reports
# both independently and correctly -- it has no concept of "these two
# belong together" (see deep.eye.oh.ext's oracle.js: circles() carries no
# entity ID by design). Left unmerged, two same-position observations are
# indistinguishable by position alone to a nearest-neighbor tracker and get
# rejected as an ambiguous match (see projectile_tracking.py/
# target_tracking.py's ambiguity-margin logic) -- not because they are
# genuinely competing hypotheses about what happened, but because they are
# two honest observations of the SAME entity. merge_colocated_circles
# collapses each such same-position, same-timestamp group into one
# representative circle (the largest radius in the group -- the entity's
# outer visual extent) before circles reach any tracker.

_COLOCATION_EPSILON_PX = 0.5  # sub-pixel; live evidence showed exact float matches, this is a small safety margin


def merge_colocated_circles(circles: tuple[BrowserCircle, ...]) -> tuple[BrowserCircle, ...]:
    """Collapses groups of circles that share the same (cx, cy) (within
    _COLOCATION_EPSILON_PX) and the same timestamp_ms into one circle each
    (the group's largest radius) -- see module comment above. A circle
    with no colocated partner passes through unchanged. Order is not
    preserved (callers must not depend on it)."""
    groups: dict[tuple[float, float, float], list[BrowserCircle]] = {}
    for circle in circles:
        key = (
            round(circle.cx / _COLOCATION_EPSILON_PX),
            round(circle.cy / _COLOCATION_EPSILON_PX),
            circle.timestamp_ms,
        )
        groups.setdefault(key, []).append(circle)
    return tuple(max(group, key=lambda c: c.radius) for group in groups.values())
