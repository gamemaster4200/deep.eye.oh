"""Custom build_py override.

The Browser Oracle extension's canonical, single checked-in source tree
lives at deep_eye_oh_ext/extension/ (a sibling of src/, imported via git
subtree from the former deep.eye.oh.ext repo) -- not inside src/deep_eye_oh/.
This hook copies it into the built package as deep_eye_oh/_extension/ during
every wheel build, so installed wheels ship the extension as real package
data (resolvable via importlib.resources) without a second, manually
synchronized checked-in copy. See src/deep_eye_oh/paths.py for the runtime
resolver, which falls back to the source tree directly for editable/dev
installs (where this hook does not run).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

REPO_ROOT = Path(__file__).parent
EXTENSION_SRC = REPO_ROOT / "deep_eye_oh_ext" / "extension"


class build_py(_build_py):
    def run(self) -> None:
        super().run()
        if not EXTENSION_SRC.is_dir():
            return
        dest = Path(self.build_lib) / "deep_eye_oh" / "_extension"
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(EXTENSION_SRC, dest)


setup(cmdclass={"build_py": build_py})
