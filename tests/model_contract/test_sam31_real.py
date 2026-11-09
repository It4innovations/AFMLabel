# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Real SAM 3.1 contract and diagnostic tests.

Marked ``real_model``: they need the ``sam`` extra, the pinned official
SAM 3 code and a downloaded manifest. They skip, never fail, when any of
that is missing so the CPU-only suite stays checkpoint independent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from afmlabel.diagnostics.models import run_backend_diagnostic
from afmlabel.segmentation.base import BackendCapability, PointPrompt
from afmlabel.segmentation.sam31 import Sam31Backend
from afmlabel.settings import RuntimeSettings, Sam31ModelSettings, load_settings
from tests.model_contract.backend_contract import run_full_contract

pytestmark = pytest.mark.real_model

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def backend() -> Sam31Backend:
    if importlib.util.find_spec("torch") is None or importlib.util.find_spec("sam3") is None:
        pytest.skip("torch / sam3 not installed")
    settings = load_settings(REPO_ROOT / "config" / "app.yaml")
    model_settings = settings.models.sam31
    checkpoint_dir = model_settings.checkpoint_dir
    if not checkpoint_dir.is_absolute():
        checkpoint_dir = REPO_ROOT / checkpoint_dir
    if not (checkpoint_dir / "manifest.json").exists():
        pytest.skip("SAM 3.1 manifest not downloaded")
    enabled = Sam31ModelSettings(
        enabled=True,
        repository=model_settings.repository,
        revision=model_settings.revision,
        checkpoint_dir=checkpoint_dir,
        code_revision=model_settings.code_revision,
        allow_cpu=model_settings.allow_cpu,
    )
    instance = Sam31Backend(enabled, RuntimeSettings(device=settings.runtime.device))
    try:
        instance.load()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"SAM 3.1 could not be loaded: {exc}")
    yield instance
    instance.release()


def test_real_sam31_contract(backend: Sam31Backend) -> None:
    descriptor = backend.descriptor()
    assert descriptor.available
    assert descriptor.checkpoint_sha256 and descriptor.model_revision and descriptor.code_revision
    assert BackendCapability.POINT_PROMPTS in descriptor.capabilities
    run_full_contract(backend)


def test_real_sam31_diagnostic(backend: Sam31Backend) -> None:
    report = run_backend_diagnostic(backend)
    assert report.status == "ok", report.model_dump()
    assert "point_prompts" in report.verified_capabilities
    assert report.peak_rss_bytes


def test_real_sam31_proposal_on_afm_like_image(backend: Sam31Backend) -> None:
    yy, xx = np.mgrid[:256, :256].astype(np.float64)
    blob = np.exp(-(((xx - 128) ** 2 + (yy - 100) ** 2) / (2 * 30.0**2)))
    image = (blob * 50000 + xx * 40).astype(np.uint16)
    proposals = backend.segment(
        image,
        points=[PointPrompt(128, 100, True)],
        box_xyxy=None,
        previous_mask=None,
        text_prompt=None,
    )
    assert proposals and proposals[0].mask.shape == (256, 256)
    assert proposals[0].mask[100, 128]
    assert proposals[0].score is not None
