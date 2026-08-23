"""Minimal CLI for manual capture/replay/control smoke testing.

    python -m deep_eye_oh.cli capture --left 0 --top 0 --width 800 --height 600 --duration 8 --out replays\r1
    python -m deep_eye_oh.cli replay --in replays\r1
    python -m deep_eye_oh.cli replay-inspect --in replays\r1 --frame 0 --save frame0.png
    python -m deep_eye_oh.cli replay-timing --in replays\r1
    python -m deep_eye_oh.cli capture-inspect --left 0 --top 0 --width 800 --height 600 --save live.png
    python -m deep_eye_oh.cli control-smoke-test
    python -m deep_eye_oh.cli coordinate-gate --in geometry.json
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from deep_eye_oh.capture import ScreenCapture
from deep_eye_oh.observation import Viewport
from deep_eye_oh.replay import ReplayReader, ReplayWriter


def _fps_stats(count: int, start: float | None, end: float | None) -> tuple[float, float]:
    """Return (duration_seconds, fps) from frame count and first/last monotonic times."""
    if count < 2 or start is None or end is None:
        return 0.0, 0.0
    duration = end - start
    fps = (count - 1) / duration if duration > 0 else 0.0
    return duration, fps


def _cmd_capture(args: argparse.Namespace) -> None:
    viewport = Viewport(left=args.left, top=args.top, width=args.width, height=args.height)
    out_path = Path(args.out)

    # Phase A: raw capture — grab only, nothing written to disk. Measures
    # true screen-grab throughput, uncontaminated by PNG encode / disk I/O.
    raw_capturer = ScreenCapture(viewport)
    raw_count = 0
    raw_start = raw_end = None
    for obs in raw_capturer.grab_stream(duration_s=args.duration):
        if raw_start is None:
            raw_start = obs.monotonic
        raw_end = obs.monotonic
        raw_count += 1
    raw_duration, raw_fps = _fps_stats(raw_count, raw_start, raw_end)

    # Phase B: the actual recording pipeline — grab, PNG-encode, write, per
    # frame. This is real end-to-end throughput, not raw capture speed:
    # encode/write time lands in the gap before the next grab.
    rec_capturer = ScreenCapture(viewport)
    rec_count = 0
    rec_start = rec_end = None
    with ReplayWriter(out_path, viewport) as writer:
        for obs in rec_capturer.grab_stream(duration_s=args.duration):
            writer.write(obs)
            if rec_start is None:
                rec_start = obs.monotonic
            rec_end = obs.monotonic
            rec_count += 1
    rec_duration, rec_fps = _fps_stats(rec_count, rec_start, rec_end)

    print(
        f"viewport: left={viewport.left} top={viewport.top} "
        f"width={viewport.width} height={viewport.height}"
    )
    print(
        f"raw capture (grab only, no encode/write): {raw_count} frames in "
        f"{raw_duration:.3f}s -> {raw_fps:.2f} fps"
    )
    print(
        f"capture+recording throughput (grab + PNG encode + write): "
        f"{rec_count} frames in {rec_duration:.3f}s -> {rec_fps:.2f} fps"
    )
    print(f"replay written to: {out_path}")


def _cmd_replay(args: argparse.Namespace) -> None:
    in_path = Path(args.in_)
    reader = ReplayReader(in_path)

    replayed = 0
    first_ts = last_ts = None
    first_mono = last_mono = None
    for obs in reader:
        if first_ts is None:
            first_ts = obs.timestamp
            first_mono = obs.monotonic
        last_ts = obs.timestamp
        last_mono = obs.monotonic
        replayed += 1

    print(
        f"viewport: left={reader.viewport.left} top={reader.viewport.top} "
        f"width={reader.viewport.width} height={reader.viewport.height}"
    )
    print(f"frames replayed: {replayed} (manifest frame_count: {reader.frame_count})")
    if replayed != reader.frame_count:
        print(
            "WARNING: replayed frame count does not match manifest "
            "frame_count — replay may be corrupt or truncated"
        )
    if replayed >= 2:
        print(f"wall-clock span: {last_ts - first_ts:.3f}s")
        print(f"monotonic span: {last_mono - first_mono:.3f}s")


def _cmd_replay_inspect(args: argparse.Namespace) -> None:
    reader = ReplayReader(Path(args.in_))
    obs = None
    for i, candidate in enumerate(reader):
        if i == args.frame:
            obs = candidate
            break
    if obs is None:
        print(f"ERROR: frame {args.frame} out of range (frame_count={reader.frame_count})")
        return

    print(f"frame_index: {obs.frame_index}")
    print(f"timestamp: {obs.timestamp}")
    print(f"monotonic: {obs.monotonic}")
    print(
        f"viewport: left={obs.viewport.left} top={obs.viewport.top} "
        f"width={obs.viewport.width} height={obs.viewport.height}"
    )
    print(f"frame: shape={obs.frame.shape} dtype={obs.frame.dtype}")

    if args.save:
        from PIL import Image

        Image.fromarray(obs.frame).save(args.save)
        print(f"saved to: {args.save}")


def _cmd_replay_timing(args: argparse.Namespace) -> None:
    reader = ReplayReader(Path(args.in_))
    monotonics = [obs.monotonic for obs in reader]
    print(f"frames read: {len(monotonics)} (manifest frame_count: {reader.frame_count})")
    if len(monotonics) < 2:
        print("not enough frames to compute timing statistics")
        return

    deltas = [b - a for a, b in zip(monotonics, monotonics[1:])]
    mean_dt = statistics.fmean(deltas)
    stdev_dt = statistics.pstdev(deltas) if len(deltas) > 1 else 0.0
    print(
        f"delta t (s): min={min(deltas):.4f} max={max(deltas):.4f} "
        f"mean={mean_dt:.4f} stdev={stdev_dt:.4f}"
    )
    if mean_dt > 0:
        print(f"implied fps (from mean delta): {1.0 / mean_dt:.2f}")


def _cmd_capture_inspect(args: argparse.Namespace) -> None:
    viewport = Viewport(left=args.left, top=args.top, width=args.width, height=args.height)
    capturer = ScreenCapture(viewport)
    obs = capturer.grab_one(frame_index=0)

    print(f"timestamp: {obs.timestamp}")
    print(f"monotonic: {obs.monotonic}")
    print(
        f"viewport: left={viewport.left} top={viewport.top} "
        f"width={viewport.width} height={viewport.height}"
    )
    print(f"frame: shape={obs.frame.shape} dtype={obs.frame.dtype}")

    if args.save:
        from PIL import Image

        Image.fromarray(obs.frame).save(args.save)
        print(f"saved to: {args.save}")


def _cmd_control_smoke_test(args: argparse.Namespace) -> None:
    from deep_eye_oh.smoke_test import run_menu

    run_menu()


def _cmd_coordinate_gate(args: argparse.Namespace) -> None:
    from deep_eye_oh.coordinate_gate import CoordinateGateError, run

    try:
        run(args.in_, countdown_s=args.countdown)
    except CoordinateGateError as exc:
        print(f"[gate] refused: {exc}")
        raise SystemExit(1) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deep_eye_oh", description="capture/replay v0 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_capture = sub.add_parser("capture", help="capture a screen region and record a replay")
    p_capture.add_argument("--left", type=int, required=True)
    p_capture.add_argument("--top", type=int, required=True)
    p_capture.add_argument("--width", type=int, required=True)
    p_capture.add_argument("--height", type=int, required=True)
    p_capture.add_argument("--duration", type=float, required=True, help="seconds")
    p_capture.add_argument("--out", type=str, required=True, help="output replay directory")
    p_capture.set_defaults(func=_cmd_capture)

    p_replay = sub.add_parser("replay", help="read a replay back and print stats")
    p_replay.add_argument("--in", dest="in_", type=str, required=True, help="replay directory")
    p_replay.set_defaults(func=_cmd_replay)

    p_replay_inspect = sub.add_parser(
        "replay-inspect", help="inspect a single frame from a stored replay"
    )
    p_replay_inspect.add_argument("--in", dest="in_", type=str, required=True)
    p_replay_inspect.add_argument("--frame", type=int, required=True)
    p_replay_inspect.add_argument("--save", type=str, default=None, help="save frame as PNG")
    p_replay_inspect.set_defaults(func=_cmd_replay_inspect)

    p_replay_timing = sub.add_parser(
        "replay-timing", help="per-frame timing/jitter statistics for a stored replay"
    )
    p_replay_timing.add_argument("--in", dest="in_", type=str, required=True)
    p_replay_timing.set_defaults(func=_cmd_replay_timing)

    p_capture_inspect = sub.add_parser(
        "capture-inspect", help="single-shot live capture of one Observation"
    )
    p_capture_inspect.add_argument("--left", type=int, required=True)
    p_capture_inspect.add_argument("--top", type=int, required=True)
    p_capture_inspect.add_argument("--width", type=int, required=True)
    p_capture_inspect.add_argument("--height", type=int, required=True)
    p_capture_inspect.add_argument("--save", type=str, default=None, help="save frame as PNG")
    p_capture_inspect.set_defaults(func=_cmd_capture_inspect)

    p_control_smoke = sub.add_parser(
        "control-smoke-test", help="interactive manual Windows control smoke-test menu"
    )
    p_control_smoke.set_defaults(func=_cmd_control_smoke_test)

    p_coordinate_gate = sub.add_parser(
        "coordinate-gate",
        help="live, non-clicking verification that an Oracle canvas point maps to the correct cursor position",
    )
    p_coordinate_gate.add_argument(
        "--in", dest="in_", type=str, required=True,
        help="path to a JSON geometry payload from the browser probe, or '-' for stdin",
    )
    p_coordinate_gate.add_argument(
        "--countdown", type=float, default=5.0,
        help="seconds to switch to the target window before arming (default 5)",
    )
    p_coordinate_gate.set_defaults(func=_cmd_coordinate_gate)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
