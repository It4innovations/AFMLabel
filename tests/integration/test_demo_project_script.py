# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""scripts/create_demo_project.py must keep working against the current stores."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from afmlabel.projects.store import ProjectStore

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "create_demo_project.py"


def test_demo_project_script_creates_a_project_with_datasets(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--workspace", str(workspace), "--pairs", "2"]
        + ["--size", "64"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "with 2 dataset(s)" in result.stdout
    projects = ProjectStore(workspace).list_projects()
    assert len(projects) == 1
