"""Exercises doctor.py: run_doctor()'s PASS/WARN/FAIL aggregation into an
overall exit code (with individual checks mocked, so this is deterministic
regardless of the real machine's state), plus the browser-cache check's
lazy-install tolerance -- a fresh install with no Chrome cache yet must
report WARN, never FAIL, since install.ps1 runs doctor as its success gate
before browser-farm has ever downloaded anything."""

from __future__ import annotations

from deep_eye_oh import browser_runtime as br
from deep_eye_oh import doctor

_ALL_CHECKS = (
    "_check_platform",
    "_check_runtime",
    "_check_control",
    "_check_bridge",
    "_check_extension",
    "_check_browser",
    "_check_profile",
)


def _check(name, status, detail=""):
    return doctor.Check(name=name, status=status, detail=detail)


def _patch_all_pass(monkeypatch):
    for name in _ALL_CHECKS:
        monkeypatch.setattr(doctor, name, lambda name=name: _check(name, "PASS"))


def test_run_doctor_ready_when_all_pass(monkeypatch, capsys):
    _patch_all_pass(monkeypatch)

    code = doctor.run_doctor()

    assert code == 0
    assert "READY" in capsys.readouterr().out


def test_run_doctor_ready_with_warn_only(monkeypatch, capsys):
    _patch_all_pass(monkeypatch)
    monkeypatch.setattr(doctor, "_check_browser", lambda: _check("browser", "WARN", "not downloaded yet"))

    code = doctor.run_doctor()

    out = capsys.readouterr().out
    assert code == 0, "a WARN-only result (e.g. Chrome not downloaded yet) must still be READY"
    assert "READY" in out
    assert "NOT READY" not in out


def test_run_doctor_not_ready_on_any_fail(monkeypatch, capsys):
    _patch_all_pass(monkeypatch)
    monkeypatch.setattr(doctor, "_check_extension", lambda: _check("extension", "FAIL", "missing"))

    code = doctor.run_doctor()

    out = capsys.readouterr().out
    assert code == 1
    assert "NOT READY" in out


def test_check_browser_warns_when_not_downloaded_yet(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    check = doctor._check_browser()
    assert check.status == "WARN"
    assert "not downloaded yet" in check.detail


def test_check_browser_passes_when_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    pin = br.load_pin()
    exe = br.chrome_exe_path(pin.version)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fake")

    check = doctor._check_browser()
    assert check.status == "PASS"


def test_check_browser_fails_on_corrupt_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    pin = br.load_pin()
    br.browser_cache_dir(pin.version).mkdir(parents=True)
    # cache dir exists, but chrome-win64/chrome.exe under it does not --
    # a corrupt/partial previous download, distinct from "never downloaded".

    check = doctor._check_browser()
    assert check.status == "FAIL"
    assert "corrupt" in check.detail
