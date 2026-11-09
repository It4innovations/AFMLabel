# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Checkpoint-independent contract tests for the ``sam3`` and ``sam21`` backends."""

from __future__ import annotations

from pathlib import Path

import pytest

from afmlabel.diagnostics.models import EXIT_UNAVAILABLE, main, run_backend_diagnostic
from afmlabel.segmentation.registry import build_registry
from afmlabel.segmentation.sam3 import Sam3Backend
from afmlabel.segmentation.sam21 import Sam21Backend
from afmlabel.settings import AppSettings, RuntimeSettings, Sam3ModelSettings, Sam21ModelSettings
from tests.model_contract.backend_contract import run_full_contract


@pytest.mark.parametrize(
    ("backend_cls", "settings_cls", "backend_id"),
    [(Sam3Backend, Sam3ModelSettings, "sam3"), (Sam21Backend, Sam21ModelSettings, "sam21")],
)
def test_disabled_backend_reports_exact_reason(backend_cls, settings_cls, backend_id) -> None:
    backend = backend_cls(settings_cls(enabled=False), RuntimeSettings())
    descriptor = backend.descriptor()
    assert descriptor.backend_id == backend_id
    assert descriptor.available is False
    assert f"models.{backend_id}.enabled" in (descriptor.unavailable_reason or "")
    assert descriptor.capabilities == frozenset()
    run_full_contract(backend)


@pytest.mark.parametrize(
    ("backend_cls", "settings_cls", "backend_id"),
    [(Sam3Backend, Sam3ModelSettings, "sam3"), (Sam21Backend, Sam21ModelSettings, "sam21")],
)
def test_enabled_backend_without_manifest_reports_reason(
    tmp_path: Path, backend_cls, settings_cls, backend_id
) -> None:
    backend = backend_cls(settings_cls(enabled=True, checkpoint_dir=tmp_path), RuntimeSettings())
    reason = backend.descriptor().unavailable_reason or ""
    assert "not installed" in reason or "manifest not found" in reason
    assert f"--model {backend_id}" in reason or "not installed" in reason
    run_full_contract(backend)


def test_manifest_revision_mismatch_is_reported(tmp_path: Path) -> None:
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    (tmp_path / "manifest.json").write_text(
        """{
  "schema_version": 1,
  "model_id": "sam21",
  "repository": "facebook/sam2.1-hiera-base-plus",
  "revision": "0000000000000000000000000000000000000000",
  "downloaded_at": "2026-09-04T00:00:00+00:00",
  "files": [],
  "checkpoint_file": "model.safetensors",
  "checkpoint_sha256": null,
  "code_revision": null,
  "license_note": null
}""",
        encoding="utf-8",
    )
    backend = Sam21Backend(
        Sam21ModelSettings(enabled=True, checkpoint_dir=tmp_path, revision="a" * 40),
        RuntimeSettings(),
    )
    reason = backend.descriptor().unavailable_reason or ""
    assert "does not match" in reason or "invalid" in reason


def test_registry_lists_all_backends_with_reasons() -> None:
    registry = build_registry(AppSettings())
    ids = {d.backend_id: d for d in registry.list_descriptors()}
    assert set(ids) >= {"fake", "sam3", "sam31", "sam21"}
    for backend_id in ("sam3", "sam31", "sam21"):
        assert ids[backend_id].available is False
        assert ids[backend_id].unavailable_reason


def test_diagnostic_exit_codes_for_unavailable_backends(tmp_path: Path) -> None:
    report = run_backend_diagnostic(Sam3Backend(Sam3ModelSettings(), RuntimeSettings()))
    assert report.status == "unavailable" and report.exit_code == EXIT_UNAVAILABLE
    report = run_backend_diagnostic(Sam21Backend(Sam21ModelSettings(), RuntimeSettings()))
    assert report.status == "unavailable" and report.exit_code == EXIT_UNAVAILABLE
    assert main(["--backend", "sam3", "--config", str(tmp_path / "none.yaml")]) == 2
    assert main(["--backend", "sam21", "--config", str(tmp_path / "none.yaml")]) == 2


def test_settings_defaults_for_new_backends() -> None:
    settings = AppSettings()
    assert settings.models.sam3.repository == "facebook/sam3"
    assert settings.models.sam3.allow_cpu is False
    assert settings.models.sam21.repository == "facebook/sam2.1-hiera-base-plus"
    assert settings.models.sam21.allow_cpu is True
    with pytest.raises(ValueError):
        Sam21ModelSettings(revision="main")
