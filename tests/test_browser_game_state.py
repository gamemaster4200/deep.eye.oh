"""Tests for browser_game_state.py: bridge message parsing (strict,
fail-closed on any malformed field) and the Oracle-canvas-pixel ->
physical-screen-pixel coordinate transform."""

import pytest

from deep_eye_oh.browser_game_state import (
    BrowserGameState,
    CanvasInfo,
    InvalidSnapshotError,
    compute_screen_transform,
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
