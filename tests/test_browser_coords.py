"""Pure-math tests for browser_coords.py -- no Win32 calls, no live browser,
deterministic synthetic geometry only."""

import pytest

from deep_eye_oh.browser_coords import (
    BrowserGeometry,
    browser_point_to_controller_point,
    canvas_point_to_css_viewport_point,
    css_viewport_point_to_client_physical_offset,
)


def _geometry(**overrides):
    defaults = dict(
        canvas_width=1920,
        canvas_height=1080,
        canvas_rect_left=0,
        canvas_rect_top=0,
        canvas_rect_width=1920,
        canvas_rect_height=1080,
        device_pixel_ratio=1.0,
        inner_width=1920,
        inner_height=1080,
        outer_width=1920,
        outer_height=1080,
    )
    defaults.update(overrides)
    return BrowserGeometry(**defaults)


# --- BrowserGeometry validation ------------------------------------------


def test_zero_css_rect_width_raises():
    with pytest.raises(ValueError, match="CSS size"):
        _geometry(canvas_rect_width=0)


def test_zero_css_rect_height_raises():
    with pytest.raises(ValueError, match="CSS size"):
        _geometry(canvas_rect_height=0)


def test_zero_backing_store_size_raises():
    with pytest.raises(ValueError, match="backing-store"):
        _geometry(canvas_width=0)


def test_zero_device_pixel_ratio_raises():
    with pytest.raises(ValueError, match="devicePixelRatio"):
        _geometry(device_pixel_ratio=0)


def test_negative_device_pixel_ratio_raises():
    with pytest.raises(ValueError, match="devicePixelRatio"):
        _geometry(device_pixel_ratio=-1)


# --- scale_x/scale_y -------------------------------------------------------


def test_scale_matches_dpr_when_canvas_css_size_equals_backing_store_over_dpr():
    # Standard high-DPI canvas pattern: backing store = CSS size * DPR.
    geo = _geometry(
        canvas_width=2400, canvas_height=1350,
        canvas_rect_width=1920, canvas_rect_height=1080,
        device_pixel_ratio=1.25,
    )
    assert geo.scale_x == pytest.approx(1.25)
    assert geo.scale_y == pytest.approx(1.25)


def test_scale_measured_independently_of_dpr_when_they_diverge():
    # Deliberately mismatched -- e.g. letterboxed/non-fullscreen canvas.
    # scale_x/scale_y must reflect the ACTUAL ratio, not devicePixelRatio.
    geo = _geometry(
        canvas_width=800, canvas_height=600,
        canvas_rect_width=400, canvas_rect_height=300,
        device_pixel_ratio=1.0,
    )
    assert geo.scale_x == pytest.approx(2.0)
    assert geo.scale_y == pytest.approx(2.0)


# --- chrome offset -----------------------------------------------------


def test_chrome_offset_zero_when_outer_equals_inner_fullscreen():
    geo = _geometry(inner_width=1920, inner_height=1080, outer_width=1920, outer_height=1080)
    assert geo.chrome_offset_top_css == 0
    assert geo.chrome_offset_left_right_css == 0


def test_chrome_offset_top_is_full_outer_inner_delta():
    geo = _geometry(inner_height=980, outer_height=1068)
    assert geo.chrome_offset_top_css == pytest.approx(88)


def test_chrome_offset_left_right_is_half_outer_inner_delta():
    geo = _geometry(inner_width=1900, outer_width=1920)
    assert geo.chrome_offset_left_right_css == pytest.approx(10)


# --- canvas_point_to_css_viewport_point ---------------------------------


def test_canvas_to_css_identity_at_dpr_1_full_viewport_canvas():
    geo = _geometry()
    assert canvas_point_to_css_viewport_point(100, 200, geo) == (100, 200)


def test_canvas_to_css_divides_by_measured_scale():
    geo = _geometry(canvas_width=2400, canvas_height=1350, canvas_rect_width=1920, canvas_rect_height=1080)
    css_x, css_y = canvas_point_to_css_viewport_point(1200, 675, geo)
    assert css_x == pytest.approx(960)
    assert css_y == pytest.approx(540)


def test_canvas_to_css_adds_canvas_rect_offset():
    geo = _geometry(canvas_rect_left=50, canvas_rect_top=20, canvas_width=800, canvas_height=600, canvas_rect_width=800, canvas_rect_height=600)
    css_x, css_y = canvas_point_to_css_viewport_point(0, 0, geo)
    assert (css_x, css_y) == (50, 20)


# --- css_viewport_point_to_client_physical_offset -----------------------


def test_css_to_physical_multiplies_by_dpr():
    geo = _geometry(device_pixel_ratio=2.0)
    px, py = css_viewport_point_to_client_physical_offset(300, 400, geo)
    assert (px, py) == pytest.approx((600, 800))


def test_css_to_physical_adds_chrome_offset_before_dpr_multiply():
    geo = _geometry(device_pixel_ratio=2.0, inner_height=980, outer_height=1068, inner_width=1900, outer_width=1920)
    px, py = css_viewport_point_to_client_physical_offset(0, 0, geo)
    # chrome_top=88, chrome_left_right=10 -> (0+10)*2, (0+88)*2
    assert (px, py) == pytest.approx((20, 176))


# --- browser_point_to_controller_point (end-to-end) ---------------------


def test_end_to_end_identity_case_full_viewport_canvas_dpr_1_no_chrome():
    geo = _geometry()
    screen = browser_point_to_controller_point(500, 300, geo, client_origin=(0, 0))
    assert screen == (500, 300)


def test_end_to_end_with_window_offset():
    geo = _geometry()
    screen = browser_point_to_controller_point(500, 300, geo, client_origin=(1000, 200))
    assert screen == (1500, 500)


def test_end_to_end_matches_spike_report_worked_example():
    # 125% scaling, canvas backing-store = CSS * DPR, canvas fills
    # viewport, browser chrome present (tab strip + a small border).
    geo = BrowserGeometry(
        canvas_width=1920, canvas_height=1080,
        canvas_rect_left=0, canvas_rect_top=0,
        canvas_rect_width=1536, canvas_rect_height=864,
        device_pixel_ratio=1.25,
        inner_width=1536, inner_height=864,
        outer_width=1552, outer_height=952,
    )
    # A shape near the canvas center.
    screen = browser_point_to_controller_point(960, 540, geo, client_origin=(100, 50))
    # css = (768, 432); chrome_top = 88, chrome_left_right = 8
    # physical_offset = ((768+8)*1.25, (432+88)*1.25) = (970, 650)
    assert screen == (100 + 970, 50 + 650)


def test_result_is_rounded_to_integers():
    geo = _geometry(device_pixel_ratio=1.3)
    x, y = browser_point_to_controller_point(1, 1, geo, client_origin=(0, 0))
    assert isinstance(x, int)
    assert isinstance(y, int)
