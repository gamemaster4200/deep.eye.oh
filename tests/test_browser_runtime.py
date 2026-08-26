"""Exercises browser_runtime.py: the real pin config (a checked-in package
resource, safe to read for real), pure argv construction, cache-hit/skip
behavior, download+hash-verify against small in-memory fixture zips (never
a real Chrome download), and process-tree-aware teardown against a fake
Popen -- no real network, no real Chrome, no real taskkill."""

from __future__ import annotations

import hashlib
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from deep_eye_oh import browser_runtime as br


def test_load_pin_reads_real_pinned_config():
    pin = br.load_pin()
    assert pin.version
    assert pin.version in pin.url
    assert len(pin.sha256) == 64


def test_build_chrome_argv_contains_expected_flags():
    chrome_exe = Path("C:/fake/chrome-win64/chrome.exe")
    extension_dir = Path("C:/fake/extension")
    profile = Path("C:/fake/profile")

    argv = br.build_chrome_argv(chrome_exe, extension_dir, profile=profile)

    assert argv[0] == str(chrome_exe)
    assert f"--user-data-dir={profile}" in argv
    assert "--profile-directory=Default" in argv
    assert f"--load-extension={extension_dir}" in argv
    assert f"--disable-extensions-except={extension_dir}" in argv
    assert "--no-first-run" in argv
    assert "--no-default-browser-check" in argv
    assert "--disable-sync" in argv
    assert "--disable-background-networking" in argv
    assert "--disable-session-crashed-bubble" in argv
    assert argv[-1] == br.DIEP_URL


def test_prepare_profile_for_launch_on_missing_preferences_creates_a_minimal_one(tmp_path):
    # A brand-new profile has no crash/session state to begin with --
    # writing a minimal Preferences file with the clean markers is
    # harmless and avoids depending on Chrome's own not-yet-created
    # defaults.
    profile = tmp_path / "profile"
    profile.mkdir()
    br._prepare_profile_for_launch(profile)  # must not raise
    prefs_path = profile / "Default" / "Preferences"
    assert prefs_path.exists()
    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data["profile"]["exit_type"] == "Normal"
    assert data["session"]["restore_on_startup"] == 5


def test_prepare_profile_for_launch_patches_existing_preferences(tmp_path):
    profile = tmp_path / "profile"
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(
        json.dumps({"profile": {"exit_type": "Crashed", "exited_cleanly": False}, "other": {"untouched": 1}}),
        encoding="utf-8",
    )

    br._prepare_profile_for_launch(profile)

    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data["profile"]["exit_type"] == "Normal"
    assert data["profile"]["exited_cleanly"] is True
    assert data["other"]["untouched"] == 1


def test_prepare_profile_for_launch_creates_profile_section_if_absent(tmp_path):
    profile = tmp_path / "profile"
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(json.dumps({"other": {}}), encoding="utf-8")

    br._prepare_profile_for_launch(profile)

    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data["profile"]["exit_type"] == "Normal"
    assert data["profile"]["exited_cleanly"] is True


def test_prepare_profile_for_launch_disables_session_restore(tmp_path):
    # Regression: Chrome's own "continue where you left off" can silently
    # reopen every diep.io tab from every previous killed run of this
    # profile -- live-observed directly (several old, already-dead tabs
    # reopening alongside the freshly launched one). Must be forced off
    # (restore_on_startup=5, "open the New Tab page") regardless of
    # whatever a previous session left behind.
    profile = tmp_path / "profile"
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(
        json.dumps({"session": {"restore_on_startup": 1, "startup_urls": ["https://diep.io/", "https://diep.io/"]}}),
        encoding="utf-8",
    )

    br._prepare_profile_for_launch(profile)

    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data["session"]["restore_on_startup"] == 5
    assert data["session"]["startup_urls"] == []


def test_prepare_profile_for_launch_on_corrupt_preferences_is_a_noop(tmp_path):
    profile = tmp_path / "profile"
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text("{not valid json", encoding="utf-8")

    br._prepare_profile_for_launch(profile)  # must not raise, must not corrupt further

    assert prefs_path.read_text(encoding="utf-8") == "{not valid json"


def test_launch_chrome_prepares_profile_for_launch(monkeypatch, tmp_path):
    profile = tmp_path / "profile"
    prefs_path = profile / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(
        json.dumps({"profile": {"exit_type": "Crashed"}, "session": {"restore_on_startup": 1}}),
        encoding="utf-8",
    )

    popen_calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv: popen_calls.append(argv) or object())

    br.launch_chrome(Path("chrome.exe"), Path("ext"), profile=profile)

    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert data["profile"]["exit_type"] == "Normal"
    assert data["session"]["restore_on_startup"] == 5
    assert len(popen_calls) == 1


def test_build_chrome_argv_custom_url():
    argv = br.build_chrome_argv(
        Path("chrome.exe"), Path("ext"), profile=Path("profile"), url="https://example.test/"
    )
    assert argv[-1] == "https://example.test/"


def _fake_pin(tmp_path: Path, *, version="999.0.0.0") -> br.ChromePin:
    return br.ChromePin(version=version, url="https://example.invalid/chrome-win64.zip", sha256="deadbeef")


def _build_fixture_zip(dest: Path) -> bytes:
    """A tiny zip whose extraction layout matches a real Chrome for Testing
    archive closely enough for find_or_download_chrome's own checks:
    chrome-win64/chrome.exe must exist after extraction."""
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("chrome-win64/chrome.exe", b"not a real binary, just a fixture")
    return dest.read_bytes()


def test_find_or_download_chrome_skips_network_on_cache_hit(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    pin = _fake_pin(tmp_path)
    exe = br.chrome_exe_path(pin.version)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"cached")

    def _boom(*a, **k):
        raise AssertionError("must not attempt a download on a cache hit")

    monkeypatch.setattr(br, "_download", _boom)

    result = br.find_or_download_chrome(pin)
    assert result == exe


def test_find_or_download_chrome_rejects_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    pin = _fake_pin(tmp_path)

    fixture = tmp_path / "fixture.zip"
    _build_fixture_zip(fixture)

    def _fake_download(url, dest):
        dest.write_bytes(fixture.read_bytes())

    monkeypatch.setattr(br, "_download", _fake_download)
    # pin.sha256 ("deadbeef") deliberately does not match the fixture zip's
    # real hash.

    with pytest.raises(br.ChromeIntegrityError, match="SHA256"):
        br.find_or_download_chrome(pin)

    assert not br.chrome_exe_path(pin.version).exists()
    assert not (br.browser_cache_dir(pin.version) / "chrome-win64.zip").exists(), "a rejected download must be removed, not left cached"


def test_find_or_download_chrome_extracts_on_verified_hash(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    fixture = tmp_path / "fixture.zip"
    fixture_bytes = _build_fixture_zip(fixture)
    real_hash = hashlib.sha256(fixture_bytes).hexdigest()
    pin = br.ChromePin(version="999.0.0.0", url="https://example.invalid/chrome-win64.zip", sha256=real_hash)

    def _fake_download(url, dest):
        dest.write_bytes(fixture_bytes)

    monkeypatch.setattr(br, "_download", _fake_download)

    result = br.find_or_download_chrome(pin)
    assert result == br.chrome_exe_path(pin.version)
    assert result.is_file()
    assert result.read_bytes() == b"not a real binary, just a fixture"
    assert not (br.browser_cache_dir(pin.version) / "chrome-win64.zip").exists(), "the zip must be cleaned up after extraction"


class FakeProcess:
    def __init__(self, pid=1234, exit_after_kill=True):
        self.pid = pid
        self._exited = False
        self._exit_after_kill = exit_after_kill

    def poll(self):
        return 0 if self._exited else None

    def wait(self, timeout=None):
        if self._exit_after_kill:
            self._exited = True
        return 0 if self._exited else None


def test_terminate_chrome_skips_taskkill_when_already_exited(monkeypatch):
    process = FakeProcess()
    process._exited = True  # already gone before teardown runs

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))

    br.terminate_chrome(process)

    assert calls == [], "must not run taskkill against a process that already exited"


def test_terminate_chrome_kills_tree_immediately_while_alive(monkeypatch):
    process = FakeProcess()  # still "alive" (poll() is None) until killed

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    br.terminate_chrome(process)

    assert calls == [["taskkill", "/PID", str(process.pid), "/T", "/F"]]
    assert process._exited is True, "wait() must be called to confirm the tree-kill completed"
