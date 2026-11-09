# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import pytest

from afmlabel.diagnostics.models import EXIT_UNAVAILABLE, main, run_backend_diagnostic
from afmlabel.segmentation.sam31 import Sam31Backend
from afmlabel.settings import RuntimeSettings, Sam31ModelSettings
from tests.model_contract.backend_contract import run_full_contract


def test_disabled_sam31_reports_exact_reason() -> None:
    backend = Sam31Backend(Sam31ModelSettings(enabled=False), RuntimeSettings())
    descriptor = backend.descriptor()
    assert descriptor.available is False
    assert "disabled in configuration" in (descriptor.unavailable_reason or "")
    run_full_contract(backend)


def test_enabled_sam31_without_model_stack_reports_missing_dependency(tmp_path: Path) -> None:
    backend = Sam31Backend(
        Sam31ModelSettings(enabled=True, checkpoint_dir=tmp_path), RuntimeSettings()
    )
    reason = backend.descriptor().unavailable_reason or ""
    assert "not installed" in reason or "manifest not found" in reason
    run_full_contract(backend)


def test_diagnostic_exit_code_for_unavailable_backend() -> None:
    report = run_backend_diagnostic(Sam31Backend(Sam31ModelSettings(), RuntimeSettings()))
    assert report.status == "unavailable"
    assert report.exit_code == EXIT_UNAVAILABLE


def test_diagnostic_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--backend", "fake", "--config", str(tmp_path / "none.yaml")]) == 0
    assert "verified capabilities" in capsys.readouterr().out
    assert main(["--backend", "sam31", "--config", str(tmp_path / "none.yaml"), "--json"]) == 2
    assert main(["--backend", "nope", "--config", str(tmp_path / "none.yaml")]) == 1
