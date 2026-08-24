"""Exercises paths.py's extension-directory resolution: the packaged-path
branch (mocked, since this dev checkout is never a built wheel), the
dev-tree fallback branch (real -- this checkout genuinely has
deep_eye_oh_ext/extension/ on disk), working-directory independence, and
the not-found error path."""

from __future__ import annotations

from pathlib import Path

import pytest

from deep_eye_oh import paths


def test_resolve_extension_dir_finds_dev_tree_by_default():
    # No packaged _extension/ exists in an editable/dev install, so this
    # must fall back to the real, checked-in deep_eye_oh_ext/extension/.
    result = paths.resolve_extension_dir()
    assert result.is_dir()
    assert result.name == "extension"
    assert (result / "manifest.json").is_file()


def test_resolve_extension_dir_independent_of_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    result = paths.resolve_extension_dir()
    assert (result / "manifest.json").is_file()


def test_packaged_extension_dir_used_when_present(monkeypatch, tmp_path):
    packaged = tmp_path / "_extension"
    packaged.mkdir()
    (packaged / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(paths.importlib.resources, "files", lambda name: tmp_path)

    result = paths.resolve_extension_dir()
    assert result == packaged


def test_packaged_extension_dir_none_when_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(paths.importlib.resources, "files", lambda name: tmp_path)
    assert paths._packaged_extension_dir() is None


def test_dev_tree_extension_dir_none_when_not_found(monkeypatch, tmp_path):
    fake_module_file = tmp_path / "somewhere" / "deep_eye_oh" / "paths.py"
    fake_module_file.parent.mkdir(parents=True)
    fake_module_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(paths, "__file__", str(fake_module_file))
    assert paths._dev_tree_extension_dir() is None


def test_resolve_extension_dir_raises_when_nothing_found(monkeypatch):
    monkeypatch.setattr(paths, "_packaged_extension_dir", lambda: None)
    monkeypatch.setattr(paths, "_dev_tree_extension_dir", lambda: None)
    with pytest.raises(paths.ExtensionNotFoundError):
        paths.resolve_extension_dir()
