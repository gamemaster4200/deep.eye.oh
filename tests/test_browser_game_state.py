"""Tests for browser_game_state.py: bridge message parsing (strict,
fail-closed on any malformed field) and the Oracle-canvas-pixel ->
physical-screen-pixel coordinate transform."""

import pytest

from deep_eye_oh.browser_game_state import (
    BrowserCircle,
    BrowserGameState,
    CanvasInfo,
    InvalidSnapshotError,
    compute_screen_transform,
    merge_colocated_circles,
    parse_bridge_message,
)


def _valid_circle(**overrides):
    circle = {"cx": 400.0, "cy": 300.0, "radius": 4.0, "color": "#ffffff", "timestamp": 4205.0}
    circle.update(overrides)
    return circle


def _valid_message(**overrides):
    message = {
        "type": "oracle_snapshot",
        "tabId": 7,
        "polledAtMs": 1000.0,
        "snapshot": {
            "metadata": {"oracleVersion": "0.5.0"},
            "timestamps": {"performanceNow": 4242.5, "wallClockMs": 1_700_000_000_000},
            "browser": {"devicePixelRatio": 2},
            "shapes": [
                {"class": "square", "cx": 100.0, "cy": 200.0, "radius": 10.0, "timestamp": 4200.0},
                {"class": "pentagon", "cx": 300.0, "cy": 50.0, "radius": 12.5, "timestamp": 4210.0},
            ],
            "canvas": {
                "width": 1600,
                "height": 900,
                "clientWidth": 800,
                "clientHeight": 450,
                "rect": {"left": 0, "top": 0, "width": 800, "height": 450},
                "devicePixelRatio": 2,
            },
        },
    }
    message.update(overrides)
    return message


# ---------------------------------------------------------------------------
# parse_bridge_message: the happy path
# ---------------------------------------------------------------------------


def test_parses_valid_message():
    state = parse_bridge_message(_valid_message(), received_at=123.0)
    assert isinstance(state, BrowserGameState)
    assert len(state.shapes) == 2
    assert state.shapes[0].shape_class == "square"
    assert state.shapes[0].cx == 100.0
    assert state.shapes[0].cy == 200.0
    assert state.shapes[0].radius == 10.0
    assert state.shapes[0].timestamp_ms == 4200.0
    assert state.shapes[1].shape_class == "pentagon"
    assert state.canvas == CanvasInfo(
        width=1600, height=900, rect_left=0, rect_top=0, rect_width=800, rect_height=450,
        device_pixel_ratio=2,
    )
    assert state.polled_at_ms == 1000.0
    assert state.performance_now_ms == 4242.5
    assert state.received_at == 123.0
    assert state.circles == (), "a message with no snapshot.circles key at all must parse with an empty tuple"


def test_parses_message_with_no_shapes():
    message = _valid_message()
    message["snapshot"]["shapes"] = []
    state = parse_bridge_message(message, received_at=1.0)
    assert state.shapes == ()


# ---------------------------------------------------------------------------
# parse_bridge_message: circles (additive, backward-compatible field)
# ---------------------------------------------------------------------------


def test_parses_message_with_circles():
    message = _valid_message()
    message["snapshot"]["circles"] = [_valid_circle(), _valid_circle(cx=1.0, cy=2.0, color=None)]
    state = parse_bridge_message(message, received_at=1.0)
    assert len(state.circles) == 2
    assert state.circles[0].cx == 400.0
    assert state.circles[0].cy == 300.0
    assert state.circles[0].radius == 4.0
    assert state.circles[0].color == "#ffffff"
    assert state.circles[0].timestamp_ms == 4205.0
    assert state.circles[1].color is None, "color must be optional"


def test_parses_message_without_circles_key_as_empty_tuple():
    # An older Oracle build (no circles() capability) omits this field
    # entirely -- that is "no circles observed", not malformed.
    message = _valid_message()
    assert "circles" not in message["snapshot"]
    state = parse_bridge_message(message, received_at=1.0)
    assert state.circles == ()


def test_parses_message_with_empty_circles_list():
    message = _valid_message()
    message["snapshot"]["circles"] = []
    state = parse_bridge_message(message, received_at=1.0)
    assert state.circles == ()


def test_rejects_circles_not_a_list():
    message = _valid_message()
    message["snapshot"]["circles"] = {"not": "a list"}
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


@pytest.mark.parametrize(
    "bad_circle",
    [
        {"cy": 0, "radius": 1, "timestamp": 0},  # missing cx
        {"cx": 0, "cy": 0, "timestamp": 0},  # missing radius
        {"cx": "nan", "cy": 0, "radius": 1, "timestamp": 0},  # non-numeric cx
        {"cx": 0, "cy": 0, "radius": 1, "timestamp": True},  # bool is not a number
        {"cx": 0, "cy": 0, "radius": 1, "timestamp": 0, "color": 123},  # non-string color
        "not an object",
    ],
    ids=["missing_cx", "missing_radius", "non_numeric_cx", "bool_timestamp", "non_string_color", "non_object_circle"],
)
def test_rejects_one_malformed_circle_by_rejecting_the_whole_message(bad_circle):
    # Same fail-closed contract as shapes: one bad circle rejects the WHOLE
    # snapshot, not just that entry.
    message = _valid_message()
    message["snapshot"]["circles"] = [_valid_circle(), bad_circle]
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


def test_parses_message_without_canvas():
    message = _valid_message()
    del message["snapshot"]["canvas"]
    state = parse_bridge_message(message, received_at=1.0)
    assert state.canvas is None


def test_parses_message_without_timestamps():
    message = _valid_message()
    del message["snapshot"]["timestamps"]
    state = parse_bridge_message(message, received_at=1.0)
    assert state.performance_now_ms is None


# ---------------------------------------------------------------------------
# parse_bridge_message: fail-closed on malformed input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.pop("type"),
        lambda m: m.update(type="something_else"),
        lambda m: m.pop("polledAtMs"),
        lambda m: m.update(polledAtMs="not a number"),
        lambda m: m.pop("snapshot"),
        lambda m: m.update(snapshot="not an object"),
    ],
    ids=["missing_type", "wrong_type", "missing_polledAtMs", "non_numeric_polledAtMs", "missing_snapshot", "non_object_snapshot"],
)
def test_rejects_malformed_envelope(mutate):
    message = _valid_message()
    mutate(message)
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


def test_rejects_non_dict_message():
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message("not a dict", received_at=1.0)
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(["not", "a", "dict"], received_at=1.0)
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(None, received_at=1.0)


def test_rejects_shapes_not_a_list():
    message = _valid_message()
    message["snapshot"]["shapes"] = {"not": "a list"}
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


@pytest.mark.parametrize(
    "bad_shape",
    [
        {"class": "hexagon", "cx": 0, "cy": 0, "radius": 1, "timestamp": 0},
        {"cx": 0, "cy": 0, "radius": 1, "timestamp": 0},  # missing class
        {"class": "square", "cy": 0, "radius": 1, "timestamp": 0},  # missing cx
        {"class": "square", "cx": "nan", "cy": 0, "radius": 1, "timestamp": 0},  # non-numeric cx
        {"class": "square", "cx": 0, "cy": 0, "radius": 1, "timestamp": True},  # bool is not a number
        "not an object",
    ],
    ids=["unknown_class", "missing_class", "missing_cx", "non_numeric_cx", "bool_timestamp", "non_object_shape"],
)
def test_rejects_one_malformed_shape_by_rejecting_the_whole_message(bad_shape):
    # A single bad shape rejects the WHOLE snapshot -- see module docstring
    # on why this is fail-closed rather than best-effort/per-shape.
    message = _valid_message()
    message["snapshot"]["shapes"] = [message["snapshot"]["shapes"][0], bad_shape]
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


def test_rejects_canvas_missing_rect():
    message = _valid_message()
    del message["snapshot"]["canvas"]["rect"]
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


def test_rejects_canvas_non_numeric_field():
    message = _valid_message()
    message["snapshot"]["canvas"]["width"] = "wide"
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


def test_canvas_browser_chrome_offset_defaults_to_zero_when_absent():
    # An older Oracle build's canvas payload (no browserChromeWidthCss/
    # HeightCss at all) is a strictly-additive-capability gap, not a
    # malformed message -- see _optional_number's own doc comment.
    state = parse_bridge_message(_valid_message(), received_at=1.0)
    assert state.canvas.browser_chrome_width_css == 0.0
    assert state.canvas.browser_chrome_height_css == 0.0


def test_canvas_parses_browser_chrome_offset_when_present():
    message = _valid_message()
    message["snapshot"]["canvas"]["browserChromeWidthCss"] = 8.0
    message["snapshot"]["canvas"]["browserChromeHeightCss"] = 143.0
    state = parse_bridge_message(message, received_at=1.0)
    assert state.canvas.browser_chrome_width_css == 8.0
    assert state.canvas.browser_chrome_height_css == 143.0


def test_rejects_canvas_non_numeric_browser_chrome_offset():
    message = _valid_message()
    message["snapshot"]["canvas"]["browserChromeHeightCss"] = "tall"
    with pytest.raises(InvalidSnapshotError):
        parse_bridge_message(message, received_at=1.0)


# ---------------------------------------------------------------------------
# compute_screen_transform
# ---------------------------------------------------------------------------


def _canvas(**overrides):
    fields = dict(
        width=1600, height=900, rect_left=0, rect_top=0, rect_width=800, rect_height=450,
        device_pixel_ratio=2,
    )
    fields.update(overrides)
    return CanvasInfo(**fields)


def test_transform_identity_when_canvas_fills_client_area_1to1():
    # backing 1600x900, client rect also 1600x900 physical, canvas CSS
    # rect 800x450 (i.e. devicePixelRatio 2 backing:CSS) -- so backing
    # pixels map 1:1 onto physical screen pixels.
    canvas = _canvas(rect_width=800, rect_height=450)
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 1600, 900))
    assert transform is not None
    assert transform.apply(0, 0) == (0, 0)
    assert transform.apply(1600, 900) == (1600, 900)
    assert transform.apply(800, 450) == (800, 450)


def test_transform_accounts_for_window_origin_offset():
    canvas = _canvas(rect_width=800, rect_height=450)
    transform = compute_screen_transform(canvas, client_rect=(100, 50, 1600, 900))
    assert transform.apply(0, 0) == (100, 50)
    assert transform.apply(1600, 900) == (1700, 950)


def test_transform_accounts_for_canvas_not_at_page_origin():
    # canvas starts 20 CSS px from the page's left/top edge.
    canvas = _canvas(rect_left=20, rect_top=10, rect_width=800, rect_height=450)
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 1600, 900))
    # 20 CSS px * (1600 physical / 800 CSS) = 40 physical px offset.
    assert transform.apply(0, 0) == (40, 20)


def test_transform_scales_down_when_client_smaller_than_backing():
    # A window client area smaller (in physical px) than the canvas
    # backing store -- e.g. OS scaling below 100%, or a shrunk window.
    canvas = _canvas(width=1600, height=900, rect_width=1600, rect_height=900)
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 800, 450))
    assert transform.apply(1600, 900) == (800, 450)
    assert transform.apply(800, 450) == (400, 225)


@pytest.mark.parametrize(
    "client_rect",
    [(0, 0, 0, 600), (0, 0, 800, 0), (0, 0, -1, 600)],
    ids=["zero_width", "zero_height", "negative_width"],
)
def test_transform_none_for_degenerate_client_rect(client_rect):
    assert compute_screen_transform(_canvas(), client_rect=client_rect) is None


def test_transform_none_for_degenerate_canvas():
    assert compute_screen_transform(_canvas(width=0), client_rect=(0, 0, 1600, 900)) is None
    assert compute_screen_transform(_canvas(rect_width=0), client_rect=(0, 0, 1600, 900)) is None


def test_transform_none_for_non_positive_device_pixel_ratio():
    assert compute_screen_transform(_canvas(device_pixel_ratio=0), client_rect=(0, 0, 1600, 900)) is None


# ---------------------------------------------------------------------------
# compute_screen_transform: browser chrome offset (live-smoke regression --
# see this function's own docstring). win32's client rect includes the
# browser's own tab-strip/omnibox/infobar, which sits ABOVE the actual page
# viewport; live-confirmed to otherwise compute screen points landing in
# the address bar instead of the game.
# ---------------------------------------------------------------------------


def test_transform_defaults_chrome_offset_to_zero_when_unset():
    # An older Oracle build/stored fixture that never reported the offset
    # must behave EXACTLY like before this fix -- never silently guess a
    # nonzero correction from data that never captured one.
    canvas = _canvas(rect_width=800, rect_height=450)
    assert canvas.browser_chrome_width_css == 0.0
    assert canvas.browser_chrome_height_css == 0.0
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 1600, 900))
    assert transform.apply(0, 0) == (0, 0)
    assert transform.apply(1600, 900) == (1600, 900)


def test_transform_accounts_for_browser_chrome_height_offset():
    # A canvas that fills its OWN page viewport exactly (rect == client
    # size), but the win32 client rect additionally includes 100 physical
    # px of browser chrome ABOVE that viewport -- canvas (0,0) (the very
    # top of the game) must map to screen y=100 (the top of the actual
    # page content), never y=0 (which would be inside the browser's own
    # tab strip/address bar).
    canvas = _canvas(
        width=1600, height=900, rect_width=1600, rect_height=900,
        device_pixel_ratio=1, browser_chrome_height_css=100,
    )
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 1600, 1000))
    assert transform.apply(0, 0) == (0, 100)
    assert transform.apply(1600, 900) == (1600, 1000)


def test_transform_accounts_for_browser_chrome_width_offset():
    canvas = _canvas(
        width=1600, height=900, rect_width=1600, rect_height=900,
        device_pixel_ratio=1, browser_chrome_width_css=50,
    )
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 1650, 900))
    assert transform.apply(0, 0) == (50, 0)
    assert transform.apply(1600, 900) == (1650, 900)


def test_transform_chrome_offset_converted_via_device_pixel_ratio():
    # The offset is reported in CSS px (window.outerHeight - innerHeight)
    # but the client rect is physical screen px -- must be converted via
    # devicePixelRatio, not applied as a raw 1:1 subtraction.
    canvas = _canvas(
        width=1600, height=900, rect_width=800, rect_height=450,
        device_pixel_ratio=2, browser_chrome_height_css=50,  # 50 CSS px == 100 physical px
    )
    transform = compute_screen_transform(canvas, client_rect=(0, 0, 1600, 1000))
    assert transform.apply(0, 0) == (0, 100)


def test_transform_none_when_chrome_offset_consumes_entire_client_rect():
    # Fail closed rather than dividing by zero/negative if the reported
    # chrome offset is as large as (or larger than) the whole client rect.
    canvas = _canvas(
        width=1600, height=900, rect_width=1600, rect_height=900,
        device_pixel_ratio=1, browser_chrome_height_css=900,
    )
    assert compute_screen_transform(canvas, client_rect=(0, 0, 1600, 900)) is None


# ---------------------------------------------------------------------------
# merge_colocated_circles (projectile-speed-and-lead-v0 live-smoke fix): a
# border+fill render pair at the same position/timestamp must collapse to
# one circle -- see this function's module comment for the live evidence.
# ---------------------------------------------------------------------------


def _circle(cx, cy, radius, timestamp_ms=100.0, color=None):
    return BrowserCircle(cx=cx, cy=cy, radius=radius, color=color, timestamp_ms=timestamp_ms)


def test_merge_collapses_a_colocated_border_fill_pair_to_the_larger_radius():
    border = _circle(865.209, 402.618, radius=10.54, timestamp_ms=721297.8, color="#0085a8")
    fill = _circle(865.209, 402.618, radius=7.66, timestamp_ms=721297.8, color="#00b2e1")
    merged = merge_colocated_circles((border, fill))
    assert len(merged) == 1
    assert merged[0].radius == 10.54
    assert merged[0].cx == 865.209
    assert merged[0].cy == 402.618


def test_merge_leaves_distinct_positions_unmerged():
    a = _circle(100.0, 100.0, radius=5.0)
    b = _circle(500.0, 500.0, radius=5.0)
    merged = merge_colocated_circles((a, b))
    assert len(merged) == 2
    assert set(merged) == {a, b}


def test_merge_does_not_merge_same_position_different_timestamp():
    # A genuinely stationary circle observed on two different frames must
    # NOT be collapsed -- only a same-instant render pair should be.
    t1 = _circle(100.0, 100.0, radius=5.0, timestamp_ms=100.0)
    t2 = _circle(100.0, 100.0, radius=5.0, timestamp_ms=150.0)
    merged = merge_colocated_circles((t1, t2))
    assert len(merged) == 2


def test_merge_handles_three_or_more_colocated_circles():
    a = _circle(0.0, 0.0, radius=5.0)
    b = _circle(0.0, 0.0, radius=9.0)
    c = _circle(0.0, 0.0, radius=3.0)
    merged = merge_colocated_circles((a, b, c))
    assert len(merged) == 1
    assert merged[0].radius == 9.0


def test_merge_within_sub_pixel_epsilon_still_collapses():
    a = _circle(100.0, 100.0, radius=10.0)
    b = _circle(100.05, 100.05, radius=6.0)  # sub-pixel float noise, same entity
    merged = merge_colocated_circles((a, b))
    assert len(merged) == 1


def test_merge_empty_input():
    assert merge_colocated_circles(()) == ()


def test_merge_single_circle_unchanged():
    a = _circle(1.0, 2.0, radius=3.0)
    assert merge_colocated_circles((a,)) == (a,)
