"""`deep-eye-oh doctor`: a short, prioritized environment/prerequisite
health check -- not a general diagnostic framework, just the checks that
distinguish "browser-farm will work" from "it won't, and here's why."

Chrome for Testing is downloaded lazily on first `browser-farm` run (see
browser_runtime.py), so a fresh install with no Chrome cache yet must still
report READY overall -- "not downloaded yet" is a WARN here, never a FAIL.
`install.ps1` runs this command as its installation-success gate, so this
distinction matters for real, not just cosmetically.
"""

from __future__ import annotations

import importlib
import json
import platform
import socket
import sys
from dataclasses import dataclass

from deep_eye_oh import __version__
from deep_eye_oh.paths import ExtensionNotFoundError, resolve_extension_dir


@dataclass(frozen=True)
class Check:
    name: str
    status: str  # "PASS" | "WARN" | "FAIL"
    detail: str = ""


def _check_platform() -> Check:
    if sys.platform != "win32":
        return Check("platform", "FAIL", f"deep.eye.oh is Windows-only (running on {sys.platform!r})")
    return Check("platform", "PASS", f"{platform.system()} {platform.release()} ({platform.machine()})")


def _check_runtime() -> Check:
    if sys.version_info < (3, 10):
        return Check("runtime", "FAIL", f"Python {platform.python_version()} < required 3.10")
    return Check("runtime", "PASS", f"Python {platform.python_version()}")


def _check_control() -> Check:
    try:
        import win32api  # noqa: F401

        from deep_eye_oh import win32_input
    except Exception as exc:
        return Check("control", "FAIL", f"pywin32/win32 control runtime unavailable: {exc}")
    status = win32_input.DPI_AWARENESS_STATUS
    if status == "unaware":
        return Check("control", "WARN", "process is DPI-unaware -- mouse/capture coordinates may not agree on scaled displays")
    return Check("control", "PASS", f"pywin32 ok, DPI awareness: {status}")


def _check_bridge() -> Check:
    try:
        importlib.import_module("websockets")
    except ImportError as exc:
        return Check("bridge", "FAIL", f"websockets not importable: {exc}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        try:
            probe.bind(("127.0.0.1", 8765))
        except OSError as exc:
            if getattr(exc, "winerror", None) == 10048:
                return Check("bridge", "WARN", "default port 8765 already in use (another browser-farm instance may be running)")
            return Check("bridge", "FAIL", f"could not bind local bridge port 8765: {exc}")
    return Check("bridge", "PASS", "websockets ok, port 8765 available")


def _check_extension() -> Check:
    try:
        ext_dir = resolve_extension_dir()
    except ExtensionNotFoundError as exc:
        return Check("extension", "FAIL", str(exc))
    manifest_path = ext_dir / "manifest.json"
    if not manifest_path.is_file():
        return Check("extension", "FAIL", f"manifest.json missing at {ext_dir}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return Check("extension", "FAIL", f"manifest.json unreadable: {exc}")
    return Check("extension", "PASS", f"{ext_dir} (manifest v{manifest.get('version', '?')})")


def _check_browser() -> Check:
    from deep_eye_oh import browser_runtime

    try:
        pin = browser_runtime.load_pin()
    except Exception as exc:
        return Check("browser", "FAIL", f"Chrome for Testing pin config unreadable: {exc}")

    exe = browser_runtime.chrome_exe_path(pin.version)
    if exe.is_file():
        return Check("browser", "PASS", f"Chrome for Testing {pin.version} cached at {exe}")

    cache_dir = browser_runtime.browser_cache_dir(pin.version)
    if cache_dir.exists():
        return Check(
            "browser",
            "FAIL",
            f"cache dir exists at {cache_dir} but chrome.exe is missing (corrupt/partial "
            "download) -- delete this directory and rerun `browser-farm` to re-download.",
        )
    return Check(
        "browser",
        "WARN",
        f"Chrome for Testing {pin.version} not downloaded yet -- will be fetched "
        "automatically on first `deep-eye-oh browser-farm` run.",
    )


def _check_profile() -> Check:
    from deep_eye_oh import browser_runtime

    profile = browser_runtime.profile_dir()
    try:
        profile.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Check("profile", "FAIL", f"cannot create browser profile directory {profile}: {exc}")
    return Check("profile", "PASS", str(profile))


def run_doctor() -> int:
    """Prints PASS/WARN/FAIL for each check plus a final READY/NOT READY
    line, and returns a process exit code (0 if no FAIL, 1 otherwise)."""
    print(f"deep.eye.oh {__version__}\n")

    checks = [
        _check_platform(),
        _check_runtime(),
        _check_control(),
        _check_bridge(),
        _check_extension(),
        _check_browser(),
        _check_profile(),
    ]

    width = max(len(c.name) for c in checks)
    for check in checks:
        line = f"{check.name.ljust(width)}  {check.status}"
        if check.detail:
            line += f": {check.detail}"
        print(line)

    has_fail = any(c.status == "FAIL" for c in checks)
    print()
    print("NOT READY" if has_fail else "READY")
    return 1 if has_fail else 0
