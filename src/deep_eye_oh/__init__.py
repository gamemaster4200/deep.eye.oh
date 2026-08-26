"""deep.eye.oh: single source of truth for the installed package version.

Previously a hardcoded literal here, independent of pyproject.toml's
[project].version -- confirmed live (v0.2.0 release install smoke) to
drift out of sync: the package installed correctly as 0.2.0 (pip/uv agree,
the extension manifest agreed), but this file still said "0.1.0" and
`deep-eye-oh doctor`/`--version` printed the stale value. Reading it from
the installed package's own metadata instead means there is nothing left
to remember to bump per release -- it can never drift again.
"""

from __future__ import annotations

import importlib.metadata

try:
    __version__ = importlib.metadata.version("deep-eye-oh")
except importlib.metadata.PackageNotFoundError:
    # Not pip-installed at all (e.g. running straight from a checkout via
    # PYTHONPATH without `pip install -e .`) -- every dev workflow in this
    # repo installs the package first, so this is not expected in
    # practice; a clearly-marked placeholder beats a stale guessed number.
    __version__ = "0.0.0+unknown"
