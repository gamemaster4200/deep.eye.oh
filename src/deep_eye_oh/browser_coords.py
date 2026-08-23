"""Oracle canvas point -> Controller mouse-target physical screen point.

Pipeline (see coord-calibration-v0 spike report for the full derivation
and empirical evidence this is built on):

    Oracle canvas backing-store point (cx, cy)
        |  / scale_x, scale_y   (scale_x = canvas.width / getBoundingClientRect().width,
        |                        measured, not assumed equal to devicePixelRatio)
        |  + canvas.getBoundingClientRect().left/top
        v
    CSS viewport point
        |  + chrome offset (top: outerHeight-innerHeight, assumed entirely
        |    above the viewport; left/right: (outerWidth-innerWidth)/2,
        |    assumed split evenly) -- corrects for modern (Aura) Chrome
        |    drawing the tab strip/toolbars inside the same top-level HWND
        |    as the page content, so ClientToScreen on that window alone
        |    is NOT the page viewport's origin
        |  * devicePixelRatio     (folds together OS scale AND browser
        |                          zoom -- Chrome does not expose the two
        |                          separately, and this transform does
        |                          not need to)
        v
    Physical point relative to the browser window's client-area origin
        |  + ClientToScreen(browser_hwnd, (0, 0))
        v
    Physical screen point == Controller.move_mouse(x, y) input

This last step is only correct if the CALLING PROCESS has declared
per-monitor DPI awareness (see win32_input.ensure_dpi_awareness, GitHub
issue #2) -- otherwise ClientToScreen/GetSystemMetrics/SendInput report a
Windows-virtualized, non-physical pixel space that does not match what
devicePixelRatio converts CSS pixels *into* on the browser side. Importing
deep_eye_oh.win32_input (which deep_eye_oh.control imports) already
declares this, once, best-effort, at import time.

No calibration state is persisted anywhere in this module. Every
BrowserGeometry is meant to be constructed fresh, immediately before each
use -- see its docstring on invalidation. There is no "calibrate once,
reuse" concept here by design.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BrowserGeometry:
    """A single, freshly-read snapshot of live browser-page geometry.

    Every field is a plain, standard, runtime-readable browser value (see
    the companion console probe in deep.eye.oh.ext) -- nothing here is
    calibrated, cached across calls, or persisted. Construct a new
    instance immediately before each transform call; reusing one across
    a resize/zoom/DPR/monitor change/fullscreen-toggle produces a stale,
    silently-wrong result (see the spike report's Invalidation conditions
    for the full list of what can change these values).
    """

    canvas_width: float  # canvas.width -- backing-store px
    canvas_height: float  # canvas.height -- backing-store px
    canvas_rect_left: float  # canvas.getBoundingClientRect().left -- CSS px
    canvas_rect_top: float  # canvas.getBoundingClientRect().top -- CSS px
    canvas_rect_width: float  # canvas.getBoundingClientRect().width -- CSS px
    canvas_rect_height: float  # canvas.getBoundingClientRect().height -- CSS px
    device_pixel_ratio: float  # window.devicePixelRatio
    inner_width: float  # window.innerWidth -- CSS px
    inner_height: float  # window.innerHeight -- CSS px
    outer_width: float  # window.outerWidth -- CSS px
    outer_height: float  # window.outerHeight -- CSS px

    def __post_init__(self) -> None:
        if self.canvas_rect_width <= 0 or self.canvas_rect_height <= 0:
            raise ValueError(
                "canvas has zero/negative CSS size -- not currently "
                "visible/renderable; refusing to compute a transform from it"
            )
        if self.canvas_width <= 0 or self.canvas_height <= 0:
            raise ValueError("canvas has zero/negative backing-store size")
        if self.device_pixel_ratio <= 0:
            raise ValueError("devicePixelRatio must be positive")

    @property
    def scale_x(self) -> float:
        """canvas backing-store px per CSS px, measured directly (not
        assumed equal to device_pixel_ratio -- see module docstring)."""
        return self.canvas_width / self.canvas_rect_width

    @property
    def scale_y(self) -> float:
        return self.canvas_height / self.canvas_rect_height

    @property
    def chrome_offset_top_css(self) -> float:
        """CSS px of browser chrome (tab strip/toolbars/bookmarks bar)
        above the page viewport, within the browser window's own client
        area. Assumed to be entirely above the viewport -- normal/
        maximized windowed Chrome never places browser UI below page
        content; this is 0 in fullscreen/kiosk mode, where
        outerHeight == innerHeight."""
        return self.outer_height - self.inner_height

    @property
    def chrome_offset_left_right_css(self) -> float:
        """CSS px of window border, assumed split evenly between the left
        and right edges."""
        return (self.outer_width - self.inner_width) / 2


def canvas_point_to_css_viewport_point(
    cx: float, cy: float, geometry: BrowserGeometry
) -> tuple[float, float]:
    """Oracle canvas backing-store point -> CSS viewport point."""
    css_x = geometry.canvas_rect_left + cx / geometry.scale_x
    css_y = geometry.canvas_rect_top + cy / geometry.scale_y
    return css_x, css_y


def css_viewport_point_to_client_physical_offset(
    css_x: float, css_y: float, geometry: BrowserGeometry
) -> tuple[float, float]:
    """CSS viewport point -> physical-pixel offset from the browser
    window's client-area origin (what ClientToScreen(hwnd, (0, 0))
    anchors). Requires a per-monitor-DPI-aware calling process."""
    physical_x = (css_x + geometry.chrome_offset_left_right_css) * geometry.device_pixel_ratio
    physical_y = (css_y + geometry.chrome_offset_top_css) * geometry.device_pixel_ratio
    return physical_x, physical_y


def browser_point_to_controller_point(
    cx: float,
    cy: float,
    geometry: BrowserGeometry,
    client_origin: tuple[int, int],
) -> tuple[int, int]:
    """Oracle canvas point (cx, cy) -> Controller.move_mouse physical
    screen coordinate.

    `client_origin` is ClientToScreen(browser_hwnd, (0, 0)) for the
    target browser window, read fresh alongside `geometry` -- both must
    come from the same moment in time (see BrowserGeometry's docstring on
    staleness). Neither this function nor BrowserGeometry perform any
    window lookup or Win32 call themselves -- that stays the caller's
    responsibility (see cli.py's `coordinate-gate` command), keeping this
    module pure and independently testable.
    """
    css_x, css_y = canvas_point_to_css_viewport_point(cx, cy, geometry)
    physical_x, physical_y = css_viewport_point_to_client_physical_offset(css_x, css_y, geometry)
    origin_x, origin_y = client_origin
    return round(origin_x + physical_x), round(origin_y + physical_y)
