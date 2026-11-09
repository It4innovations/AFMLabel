# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Explicit background loading of model backends through the registry."""

from __future__ import annotations

import threading

import numpy as np
import pytest

from afmlabel.segmentation.base import (
    BackendCapability,
    BackendDescriptor,
    BackendUnavailableError,
    PointPrompt,
    SegmentationProposal,
)
from afmlabel.segmentation.fake import FakeBackend
from afmlabel.segmentation.registry import BackendNotLoadableError, BackendRegistry


class SlowBackend:
    """Loadable backend whose load blocks until released, like a real model."""

    backend_id = "slow"

    def __init__(self, *, reason: str | None = None, fail: bool = False) -> None:
        self._reason = reason
        self._fail = fail
        self._loaded = False
        self._load_error: str | None = None
        self.gate = threading.Event()

    def unavailable_reason(self) -> str | None:
        return self._reason or self._load_error

    def load(self) -> None:
        self.gate.wait(5)
        if self._fail:
            self._load_error = "model load failed: synthetic failure"
            raise BackendUnavailableError(self.backend_id, self._load_error)
        self._loaded = True

    def descriptor(self) -> BackendDescriptor:
        reason = self.unavailable_reason()
        return BackendDescriptor(
            backend_id=self.backend_id,
            display_name="Slow",
            capabilities=(
                frozenset({BackendCapability.POINT_PROMPTS}) if self._loaded else frozenset()
            ),
            available=reason is None and self._loaded,
            unavailable_reason=reason or (None if self._loaded else "model not loaded yet"),
            model_repository=None,
            model_revision=None,
            checkpoint_sha256=None,
            code_revision=None,
            device=None,
            dtype=None,
        )

    def segment(
        self,
        image: np.ndarray,
        *,
        points: list[PointPrompt],
        box_xyxy: tuple[float, float, float, float] | None,
        previous_mask: np.ndarray | None,
        text_prompt: str | None,
    ) -> list[SegmentationProposal]:
        return []

    def release(self) -> None:
        self._loaded = False


def test_fake_backend_is_loaded_from_the_start() -> None:
    registry = BackendRegistry({"fake": FakeBackend()})
    assert registry.describe("fake").load_state == "loaded"
    assert registry.start_load("fake").load_state == "loaded"


def test_background_load_transitions_not_loaded_loading_loaded() -> None:
    backend = SlowBackend()
    registry = BackendRegistry({"slow": backend})
    assert registry.describe("slow").load_state == "not_loaded"
    assert registry.describe("slow").available is False

    first = registry.start_load("slow")
    assert first.load_state == "loading"
    # Idempotent while the load runs: no second thread, same state.
    assert registry.start_load("slow").load_state == "loading"
    backend.gate.set()
    assert registry.wait_for_load("slow", timeout=5) == "loaded"
    descriptor = registry.describe("slow")
    assert descriptor.available and descriptor.load_state == "loaded"


def test_failed_load_is_reported_with_reason() -> None:
    backend = SlowBackend(fail=True)
    backend.gate.set()
    registry = BackendRegistry({"slow": backend})
    registry.start_load("slow")
    assert registry.wait_for_load("slow", timeout=5) == "failed"
    descriptor = registry.describe("slow")
    assert descriptor.available is False
    assert "synthetic failure" in (descriptor.unavailable_reason or "")


def test_misconfigured_backend_is_not_loadable() -> None:
    registry = BackendRegistry({"slow": SlowBackend(reason="disabled in configuration")})
    assert registry.describe("slow").load_state == "unavailable"
    with pytest.raises(BackendNotLoadableError) as excinfo:
        registry.start_load("slow")
    assert "disabled in configuration" in excinfo.value.reason
