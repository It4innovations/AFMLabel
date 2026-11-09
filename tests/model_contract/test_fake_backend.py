# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from afmlabel.diagnostics.models import EXIT_OK, run_backend_diagnostic
from afmlabel.segmentation.base import SegmentationBackend
from afmlabel.segmentation.fake import FakeBackend
from tests.model_contract.backend_contract import run_full_contract


def test_fake_backend_satisfies_the_contract() -> None:
    backend = FakeBackend()
    assert isinstance(backend, SegmentationBackend)
    run_full_contract(backend)


def test_fake_backend_passes_the_model_diagnostic() -> None:
    report = run_backend_diagnostic(FakeBackend())
    assert report.status == "ok", report.model_dump()
    assert report.exit_code == EXIT_OK
    assert set(report.verified_capabilities) == {"point_prompts", "box_prompts", "mask_prompts"}
    assert report.peak_rss_bytes and report.peak_rss_bytes > 0
    assert any(s.name == "typed_rejection" and s.status == "ok" for s in report.steps)
