# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Common contract every segmentation backend must satisfy.

Import these checks from a backend-specific test module and call them
with a backend instance. Real-model suites are marked ``real_model``.
"""

from __future__ import annotations

import numpy as np
import pytest

from afmlabel.segmentation.base import (
    BackendCapability,
    BackendCapabilityError,
    BackendDescriptor,
    BackendUnavailableError,
    PointPrompt,
    SegmentationBackend,
)


def synthetic_image(height: int = 64, width: int = 64) -> np.ndarray:
    """Gentle gradient with one bright island centred at (32, 32).

    The island makes the semantic checks meaningful for real models: on a
    featureless gradient SAM-style models legitimately return the whole
    image even with a negative point.
    """
    yy, xx = np.mgrid[:height, :width].astype(np.float64)
    gradient = (xx + yy) * 64.0
    island = 40000.0 * np.exp(-(((xx - 32) ** 2 + (yy - 32) ** 2) / (2 * 8.0**2)))
    return np.clip(gradient + island, 0, 65535).astype(np.uint16)


def check_descriptor(backend: SegmentationBackend) -> BackendDescriptor:
    descriptor = backend.descriptor()
    assert descriptor.backend_id == backend.backend_id
    assert descriptor.display_name
    if descriptor.available:
        assert descriptor.unavailable_reason is None
    else:
        assert descriptor.unavailable_reason, "unavailable backends must give a reason"
        assert not descriptor.capabilities, "unavailable backends must not advertise capabilities"
    return descriptor


def check_unavailable_backend_raises_typed_error(backend: SegmentationBackend) -> None:
    descriptor = backend.descriptor()
    assert not descriptor.available
    with pytest.raises(BackendUnavailableError) as excinfo:
        backend.segment(
            synthetic_image(),
            points=[PointPrompt(10, 10, True)],
            box_xyxy=None,
            previous_mask=None,
            text_prompt=None,
        )
    assert excinfo.value.reason == descriptor.unavailable_reason


def check_point_prompt(backend: SegmentationBackend) -> None:
    descriptor = backend.descriptor()
    image = synthetic_image()
    if BackendCapability.POINT_PROMPTS not in descriptor.capabilities:
        with pytest.raises(BackendCapabilityError):
            backend.segment(
                image,
                points=[PointPrompt(32, 32, True)],
                box_xyxy=None,
                previous_mask=None,
                text_prompt=None,
            )
        return
    proposals = backend.segment(
        image,
        points=[PointPrompt(32, 32, True), PointPrompt(60, 60, False)],
        box_xyxy=None,
        previous_mask=None,
        text_prompt=None,
    )
    assert proposals
    for proposal in proposals:
        assert proposal.mask.shape == image.shape[:2]
        assert proposal.mask.dtype == np.bool_
        assert proposal.score is None or 0.0 <= proposal.score <= 1.0
    assert proposals[0].mask[32, 32]
    assert not proposals[0].mask[60, 60]
    if BackendCapability.MULTI_MASK_PROPOSALS in descriptor.capabilities:
        assert len(proposals) >= 1


def check_box_prompt(backend: SegmentationBackend) -> None:
    descriptor = backend.descriptor()
    image = synthetic_image()
    box = (10.0, 10.0, 30.0, 30.0)
    if BackendCapability.BOX_PROMPTS not in descriptor.capabilities:
        with pytest.raises(BackendCapabilityError):
            backend.segment(image, points=[], box_xyxy=box, previous_mask=None, text_prompt=None)
        return
    proposals = backend.segment(
        image, points=[], box_xyxy=box, previous_mask=None, text_prompt=None
    )
    assert proposals[0].mask[20, 20]
    assert not proposals[0].mask[50, 50]


def check_mask_prompt(backend: SegmentationBackend) -> None:
    descriptor = backend.descriptor()
    image = synthetic_image()
    # A coarse starting mask on the island; the model refines it around the
    # positive point. (A detached far-away blob would be a prior a real model
    # is free to drop, so the check uses the island itself.)
    yy, xx = np.mgrid[: image.shape[0], : image.shape[1]]
    previous = ((xx - 32) ** 2 + (yy - 32) ** 2) <= 6**2
    if BackendCapability.MASK_PROMPTS not in descriptor.capabilities:
        with pytest.raises(BackendCapabilityError):
            backend.segment(
                image,
                points=[PointPrompt(32, 32, True)],
                box_xyxy=None,
                previous_mask=previous,
                text_prompt=None,
            )
        return
    proposals = backend.segment(
        image,
        points=[PointPrompt(32, 32, True)],
        box_xyxy=None,
        previous_mask=previous,
        text_prompt=None,
    )
    assert proposals[0].mask[32, 32]
    assert not proposals[0].mask[7, 7]


def check_text_prompt_rejection_when_unsupported(backend: SegmentationBackend) -> None:
    descriptor = backend.descriptor()
    if BackendCapability.TEXT_PROMPTS in descriptor.capabilities:
        return
    with pytest.raises(BackendCapabilityError) as excinfo:
        backend.segment(
            synthetic_image(),
            points=[],
            box_xyxy=None,
            previous_mask=None,
            text_prompt="a layer",
        )
    assert excinfo.value.capability == BackendCapability.TEXT_PROMPTS


def check_empty_prompt_is_rejected(backend: SegmentationBackend) -> None:
    with pytest.raises((ValueError, BackendCapabilityError)):
        backend.segment(
            synthetic_image(), points=[], box_xyxy=None, previous_mask=None, text_prompt=None
        )


def check_determinism(backend: SegmentationBackend) -> None:
    descriptor = backend.descriptor()
    if BackendCapability.POINT_PROMPTS not in descriptor.capabilities:
        return
    image = synthetic_image()
    kwargs = dict(
        points=[PointPrompt(20, 20, True)], box_xyxy=None, previous_mask=None, text_prompt=None
    )
    first = backend.segment(image, **kwargs)[0].mask
    second = backend.segment(image, **kwargs)[0].mask
    assert np.array_equal(first, second)


def check_release_is_idempotent(backend: SegmentationBackend) -> None:
    backend.release()
    backend.release()
    # Releasing must not make an available backend permanently unavailable.
    descriptor = backend.descriptor()
    if descriptor.available:
        check_point_prompt(backend)


def run_full_contract(backend: SegmentationBackend) -> None:
    descriptor = check_descriptor(backend)
    if not descriptor.available:
        check_unavailable_backend_raises_typed_error(backend)
        return
    check_point_prompt(backend)
    check_box_prompt(backend)
    check_mask_prompt(backend)
    check_text_prompt_rejection_when_unsupported(backend)
    check_empty_prompt_is_rejected(backend)
    check_determinism(backend)
    check_release_is_idempotent(backend)
