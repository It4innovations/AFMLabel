# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""No Qt toolkit anywhere in the stack."""

from __future__ import annotations

import importlib.metadata
import sys
from pathlib import Path

FORBIDDEN = ("pyqt", "pyside", "qt")


def test_pyproject_declares_no_qt_dependency() -> None:
    text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text().lower()
    for name in ("pyqt", "pyside", "qtpy"):
        assert name not in text


def test_no_qt_distribution_is_installed_in_the_environment() -> None:
    names = {d.metadata["Name"].lower() for d in importlib.metadata.distributions()}
    assert not [n for n in names if n.startswith(("pyqt", "pyside", "qtpy"))]


def test_no_qt_module_is_imported_by_the_application() -> None:
    import afmlabel.main  # noqa: F401

    assert not [m for m in sys.modules if m.lower().startswith(("pyqt", "pyside", "qtpy"))]
