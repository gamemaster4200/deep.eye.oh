"""Managed Chrome for Testing acquisition, caching, launch-argv
construction, and process-tree-aware teardown for browser-farm.

Chrome for Testing (not the user's normal Chrome) is used specifically
because --load-extension and --disable-extensions-except are confirmed to
still work in Chrome-for-Testing/Chromium builds even though Chrome removed
them from Chrome-branded builds starting Chrome 137+/139+ (see the Chromium
extensions-dev PSA threads on removing --load-extension from branded
builds). The Chrome for Testing JSON API does not publish a hash for its
downloads, so integrity is verified against a maintainer-pinned SHA256
recorded once in _data/chrome_for_testing.json, not against anything the
API itself asserts.
"""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import logging
import os
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DIEP_URL = "https://diep.io/"
_DOWNLOAD_CHUNK_SIZE = 1 << 20  # 1 MiB
_TASKKILL_WAIT_S = 10.0


@dataclass(frozen=True)
class ChromePin:
    version: str
    url: str
    sha256: str


def load_pin() -> ChromePin:
    """The single canonical {version, url, sha256} pin, read from the
    installed package resource -- works identically for installed wheels
    and dev checkouts, since _data/ is an ordinary checked-in package-data
    file (unlike the build-time-generated extension tree in _extension/)."""
    raw = (
        importlib.resources.files("deep_eye_oh")
        .joinpath("_data", "chrome_for_testing.json")
        .read_text(encoding="utf-8")
    )
    data = json.loads(raw)
    return ChromePin(version=data["version"], url=data["url"], sha256=data["sha256"])


def app_data_root() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "deep-eye-oh"


def browser_cache_dir(version: str) -> Path:
    return app_data_root() / "browser" / version


def chrome_exe_path(version: str) -> Path:
    return browser_cache_dir(version) / "chrome-win64" / "chrome.exe"


def profile_dir() -> Path:
    return app_data_root() / "browser-profile"


class ChromeIntegrityError(RuntimeError):
    """The downloaded Chrome for Testing archive did not match the pinned
    SHA256, or the extracted archive did not contain chrome.exe where
    expected -- never extracted, never trusted, never silently retried."""


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    with urllib.request.urlopen(url) as response, open(tmp, "wb") as out:
        while True:
            chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
    tmp.replace(dest)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_DOWNLOAD_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def find_or_download_chrome(pin: ChromePin | None = None) -> Path:
    """The cached chrome.exe path for the pinned version, downloading,
    hash-verifying, and extracting it first if not already cached. Never
    re-downloads once chrome.exe exists at the expected cache path."""
    pin = pin or load_pin()
    exe = chrome_exe_path(pin.version)
    if exe.is_file():
        return exe

    cache_dir = browser_cache_dir(pin.version)
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / "chrome-win64.zip"
    logger.info("downloading Chrome for Testing %s ...", pin.version)
    _download(pin.url, zip_path)

    actual_hash = _sha256_of(zip_path)
    if actual_hash != pin.sha256:
        zip_path.unlink(missing_ok=True)
        raise ChromeIntegrityError(
            f"Chrome for Testing {pin.version} download did not match the "
            f"pinned SHA256 (expected {pin.sha256}, got {actual_hash}); "
            "download rejected."
        )

    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(cache_dir)
    zip_path.unlink(missing_ok=True)

    if not exe.is_file():
        raise ChromeIntegrityError(
            f"Chrome for Testing {pin.version} archive extracted but "
            f"chrome.exe was not found at the expected path {exe}."
        )
    return exe


def build_chrome_argv(
    chrome_exe: Path,
    extension_dir: Path,
    *,
    profile: Path | None = None,
    url: str = DIEP_URL,
) -> list[str]:
    """The Chrome-for-Testing launch command: a dedicated persistent
    profile, the bundled extension auto-loaded and enabled by default (not
    merely installed-but-disabled -- --load-extension alone leaves an
    unpacked extension disabled unless paired with
    --disable-extensions-except), and diep.io opened directly -- no
    first-run/default-browser/sync noise that could stall an unattended
    launch."""
    profile = profile or profile_dir()
    ext = str(extension_dir)
    return [
        str(chrome_exe),
        f"--user-data-dir={profile}",
        "--profile-directory=Default",
        f"--load-extension={ext}",
        f"--disable-extensions-except={ext}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--new-window",
        url,
    ]


def launch_chrome(
    chrome_exe: Path,
    extension_dir: Path,
    *,
    profile: Path | None = None,
    url: str = DIEP_URL,
) -> subprocess.Popen:
    profile = profile or profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    argv = build_chrome_argv(chrome_exe, extension_dir, profile=profile, url=url)
    return subprocess.Popen(argv)


def terminate_chrome(process: subprocess.Popen, *, wait_s: float = _TASKKILL_WAIT_S) -> None:
    """Kill the whole Chrome-for-Testing process tree this code spawned.

    Deliberately skips a graceful popen.terminate()-then-wait step: Chrome
    is multi-process, and if the root process exited during any grace
    period before a tree-kill ran, `taskkill /T` would lose its only valid
    tree anchor and could orphan renderer/GPU children. So: if the root
    process is still alive, kill the tree immediately via
    `taskkill /PID <pid> /T /F` and wait for it to finish. This only ever
    targets the specific pid this code itself spawned via Popen -- never an
    unrelated Chrome instance -- and the dedicated --user-data-dir keeps
    that ownership unambiguous.
    """
    if process.poll() is not None:
        return  # already exited; nothing to do
    try:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=wait_s,
        )
    except subprocess.TimeoutExpired:
        logger.warning("taskkill did not complete within %.1fs for chrome pid %s", wait_s, process.pid)
    try:
        process.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        logger.warning("chrome process %s did not exit after taskkill /T /F", process.pid)
