# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Real SAM 3 (image) and SAM 2.1 contract and diagnostic tests.

Marked ``real_model``: they need the ``sam`` extra plus the downloaded
manifests referenced from ``config/app.yaml``. They skip, never fail, when
any of that is missing so the CPU-only suite stays checkpoint independent.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from afmlabel.diagnostics.models import run_backend_diagnostic
from afmlabel.segmentation.base import BackendCapability, PointPrompt
from afmlabel.segmentation.sam3 import Sam3Backend
from afmlabel.segmentation.sam21 import Sam21Backend
from afmlabel.settings import RuntimeSettings, load_settings
from tests.model_contract.backend_contract import run_full_contract

pytestmark = pytest.mark.real_model

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_backend(backend_id: str, backend_cls: type, required_modules: tuple[str, ...]) -> Any:
    for module in required_modules:
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{module} not installed")
    settings = load_settings(REPO_ROOT / "config" / "app.yaml")
    model_settings = getattr(settings.models, backend_id)
    checkpoint_dir = model_settings.checkpoint_dir
    if not checkpoint_dir.is_absolute():
        checkpoint_dir = REPO_ROOT / checkpoint_dir
    if not (checkpoint_dir / "manifest.json").exists():
        pytest.skip(f"{backend_id} manifest not downloaded")
    enabled = model_settings.model_copy(update={"enabled": True, "checkpoint_dir": checkpoint_dir})
    instance = backend_cls(enabled, RuntimeSettings(device=settings.runtime.device))
    try:
        instance.load()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"{backend_id} could not be loaded: {exc}")
    return instance


@pytest.fixture(scope="module")
def sam3() -> Any:
    instance = _load_backend("sam3", Sam3Backend, ("torch", "sam3"))
    yield instance
    instance.release()


@pytest.fixture(scope="module")
def sam21() -> Any:
    instance = _load_backend("sam21", Sam21Backend, ("torch", "transformers"))
    yield instance
    instance.release()


def _afm_like_image() -> np.ndarray:
    yy, xx = np.mgrid[:256, :256].astype(np.float64)
    blob = np.exp(-(((xx - 128) ** 2 + (yy - 100) ** 2) / (2 * 30.0**2)))
    return (blob * 50000 + xx * 40).astype(np.uint16)


def _exercise_interactive(backend: Any) -> None:
    image = _afm_like_image()
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
    assert len(proposals) > 1, "a single positive point should yield multi-mask variants"
    scores = [p.score for p in proposals]
    assert scores == sorted(scores, reverse=True)

    seed = proposals[0].mask
    refined = backend.segment(
        image,
        points=[PointPrompt(128, 100, True), PointPrompt(20, 240, False)],
        box_xyxy=None,
        previous_mask=seed,
        text_prompt=None,
    )
    assert refined and refined[0].mask[100, 128]
    assert len(refined) == 1

    boxed = backend.segment(
        image, points=[], box_xyxy=(90.0, 60.0, 170.0, 140.0), previous_mask=None, text_prompt=None
    )
    assert boxed and boxed[0].mask[100, 128]
    assert not boxed[0].mask[10, 10]


# --- SAM 3 -----------------------------------------------------------------


def test_real_sam3_contract(sam3: Sam3Backend) -> None:
    descriptor = sam3.descriptor()
    assert descriptor.available
    assert descriptor.checkpoint_sha256 and descriptor.model_revision and descriptor.code_revision
    for capability in (
        BackendCapability.POINT_PROMPTS,
        BackendCapability.BOX_PROMPTS,
        BackendCapability.MASK_PROMPTS,
        BackendCapability.TEXT_PROMPTS,
        BackendCapability.MULTI_MASK_PROPOSALS,
    ):
        assert capability in descriptor.capabilities, descriptor.warnings
    run_full_contract(sam3)


def test_real_sam3_diagnostic(sam3: Sam3Backend) -> None:
    report = run_backend_diagnostic(sam3)
    assert report.status == "ok", report.model_dump()
    assert {"point_prompts", "box_prompts", "mask_prompts"} <= set(report.verified_capabilities)
    assert report.peak_rss_bytes


def test_real_sam3_interactive_prompts(sam3: Sam3Backend) -> None:
    _exercise_interactive(sam3)


def test_real_sam3_text_prompt_returns_typed_result(sam3: Sam3Backend) -> None:
    proposals = sam3.segment(
        _afm_like_image(), points=[], box_xyxy=None, previous_mask=None, text_prompt="bright blob"
    )
    for proposal in proposals:
        assert proposal.mask.shape == (256, 256) and proposal.mask.dtype == np.bool_
        assert proposal.backend_metadata["path"] == "concept_detector"


# --- SAM 2.1 ---------------------------------------------------------------


def test_real_sam21_contract(sam21: Sam21Backend) -> None:
    descriptor = sam21.descriptor()
    assert descriptor.available
    assert descriptor.checkpoint_sha256 and descriptor.model_revision and descriptor.code_revision
    for capability in (
        BackendCapability.POINT_PROMPTS,
        BackendCapability.BOX_PROMPTS,
        BackendCapability.MASK_PROMPTS,
        BackendCapability.MULTI_MASK_PROPOSALS,
    ):
        assert capability in descriptor.capabilities, descriptor.warnings
    assert BackendCapability.TEXT_PROMPTS not in descriptor.capabilities
    run_full_contract(sam21)


def test_real_sam21_diagnostic(sam21: Sam21Backend) -> None:
    report = run_backend_diagnostic(sam21)
    assert report.status == "ok", report.model_dump()
    assert {"point_prompts", "box_prompts", "mask_prompts"} <= set(report.verified_capabilities)


def test_real_sam21_interactive_prompts(sam21: Sam21Backend) -> None:
    _exercise_interactive(sam21)


def _cpu_backend(backend_id: str, backend_cls: type, required_modules: tuple[str, ...]) -> Any:
    for module in required_modules:
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"{module} not installed")
    settings = load_settings(REPO_ROOT / "config" / "app.yaml")
    model_settings = getattr(settings.models, backend_id)
    checkpoint_dir = model_settings.checkpoint_dir
    if not checkpoint_dir.is_absolute():
        checkpoint_dir = REPO_ROOT / checkpoint_dir
    if not (checkpoint_dir / "manifest.json").exists():
        pytest.skip(f"{backend_id} manifest not downloaded")
    return backend_cls(
        model_settings.model_copy(
            update={"enabled": True, "checkpoint_dir": checkpoint_dir, "allow_cpu": True}
        ),
        RuntimeSettings(device="cpu"),
    )


def test_real_sam21_runs_on_cpu() -> None:
    """The CPU-only deployment profile must be able to use SAM 2.1."""
    backend = _cpu_backend("sam21", Sam21Backend, ("torch", "transformers"))
    try:
        backend.load()
        descriptor = backend.descriptor()
        assert descriptor.available and descriptor.device == "cpu"
        proposals = backend.segment(
            _afm_like_image(),
            points=[PointPrompt(128, 100, True)],
            box_xyxy=None,
            previous_mask=None,
            text_prompt=None,
        )
        assert proposals and proposals[0].mask[100, 128]
    finally:
        backend.release()


def test_real_sam3_runs_on_cpu() -> None:
    """SAM 3 on CPU (float32 MLP path, lazy device caches): every prompt kind."""
    backend = _cpu_backend("sam3", Sam3Backend, ("torch", "sam3"))
    try:
        backend.load()
        descriptor = backend.descriptor()
        assert descriptor.available and descriptor.device == "cpu", descriptor.warnings
        for capability in (
            BackendCapability.POINT_PROMPTS,
            BackendCapability.BOX_PROMPTS,
            BackendCapability.MASK_PROMPTS,
            BackendCapability.TEXT_PROMPTS,
        ):
            assert capability in descriptor.capabilities, descriptor.warnings
        proposals = backend.segment(
            _afm_like_image(),
            points=[PointPrompt(128, 100, True)],
            box_xyxy=None,
            previous_mask=None,
            text_prompt=None,
        )
        assert proposals and proposals[0].mask[100, 128]
        text = backend.segment(
            _afm_like_image(), points=[], box_xyxy=None, previous_mask=None, text_prompt="blob"
        )
        for proposal in text:
            assert proposal.mask.shape == (256, 256)
    finally:
        backend.release()
