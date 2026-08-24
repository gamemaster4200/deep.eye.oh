"""Filesystem path resolution for the bundled Browser Oracle extension.

The extension is packaged in two different ways depending on how
deep_eye_oh is installed, so resolution has two branches, both ultimately
sourced from the single canonical tree at deep_eye_oh_ext/extension/ (see
setup.py):

  * Installed wheel: the extension is copied into the package as real
    on-disk files at deep_eye_oh/_extension/ (setup.py's build_py hook).
    importlib.resources resolves this to a real directory for a normal
    (non-zipped) wheel install.
  * Editable/dev install (`pip install -e .`): the build_py copy step does
    not run into src/, so deep_eye_oh/_extension/ does not exist. Fall back
    to walking up from this file to find a checked-out deep_eye_oh_ext/
    sibling directory and use its extension/ subdirectory directly.

Both branches resolve to an absolute filesystem path regardless of the
current working directory -- never derived from cwd.
"""

from __future__ import annotations

import importlib.resources
from pathlib import Path


class ExtensionNotFoundError(RuntimeError):
    """The bundled Browser Oracle extension could not be located by any
    resolution strategy -- indicates a broken install, not a normal
    runtime condition."""


def _packaged_extension_dir() -> Path | None:
    try:
        candidate = importlib.resources.files("deep_eye_oh") / "_extension"
    except ModuleNotFoundError:
        return None
    if candidate.is_dir():
        return Path(str(candidate))
    return None


def _dev_tree_extension_dir() -> Path | None:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "deep_eye_oh_ext" / "extension"
        if candidate.is_dir():
            return candidate
    return None


def resolve_extension_dir() -> Path:
    """The absolute filesystem directory of the bundled extension --
    suitable for handing directly to Chrome's --load-extension flag.
    Raises ExtensionNotFoundError rather than returning a nonexistent path.
    """
    packaged = _packaged_extension_dir()
    if packaged is not None:
        return packaged
    dev_tree = _dev_tree_extension_dir()
    if dev_tree is not None:
        return dev_tree
    raise ExtensionNotFoundError(
        "Could not locate the bundled Browser Oracle extension (checked "
        "installed package data at deep_eye_oh/_extension/ and a dev "
        "checkout sibling deep_eye_oh_ext/extension/ directory). This "
        "indicates a broken installation."
    )
