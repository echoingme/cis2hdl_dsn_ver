"""Shared path resolution for tests that operate on a real Capture design.

Several DSN-level tests are written against a reference board that is **not
redistributed** with this repository (it is a third-party design). Point the
``CIS2HDL_DSN_ROOT`` environment variable at a directory that contains
``<design>.DSN`` and its ``.EXP`` / ``.CSV`` side files to enable them:

    export CIS2HDL_DSN_ROOT=/path/to/design_files
    pytest tests/unit -q

Without the variable these tests skip rather than fail, so a fresh clone always
runs the design-independent part of the suite cleanly.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directory holding the reference design and its exported side files.
DATA_ROOT = Path(os.environ.get("CIS2HDL_DSN_ROOT", REPO_ROOT / "TG0C0D2_files"))

#: Stem of the reference design used by the DSN-level tests.
DESIGN_STEM = os.environ.get("CIS2HDL_DESIGN_STEM", "TG0C0D2_VER_B_SCH")


def design_path(suffix: str = "DSN") -> Path:
    """Return the path of a reference-design file with the given suffix."""
    return DATA_ROOT / f"{DESIGN_STEM}.{suffix}"


def data_path(name: str) -> Path:
    """Return the path of an auxiliary data file next to the reference design."""
    return DATA_ROOT / name


def require(path: Path) -> Path:
    """Return ``path`` or skip the calling test when the file is absent."""
    import pytest

    if not Path(path).exists():
        pytest.skip(f"reference design file not available: {path}")
    return Path(path)
