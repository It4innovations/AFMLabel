# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Backend-neutral segmentation protocol.

Model-specific state never leaks into canonical annotations: callers only
see :class:`BackendDescriptor`, :class:`SegmentationProposal` and the
prompt dataclasses defined here. A backend that does not support a
requested prompt kind must raise :class:`BackendCapabilityError`; a backend
whose descriptor reports ``available=False`` must raise
:class:`BackendUnavailableError` from every inference call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

import numpy as np


class BackendCapability(StrEnum):
    POINT_PROMPTS = "point_prompts"
    BOX_PROMPTS = "box_prompts"
    MASK_PROMPTS = "mask_prompts"
    TEXT_PROMPTS = "text_prompts"
    EXEMPLAR_PROMPTS = "exemplar_prompts"
    MULTI_MASK_PROPOSALS = "multi_mask_proposals"


@dataclass(frozen=True)
class BackendDescriptor:
    backend_id: str
    display_name: str
    capabilities: frozenset[BackendCapability]
    available: bool
    unavailable_reason: str | None
    model_repository: str | None
    model_revision: str | None
    checkpoint_sha256: str | None
    code_revision: str | None
    device: str | None
    dtype: str | None
    #: Human-readable caveats (e.g. "CPU inference is slow"). Never blocks use.
    warnings: tuple[str, ...] = ()
    #: ``loaded`` (usable now), ``not_loaded`` (configured and verifiable,
    #: load it with ``POST /api/v1/models/{id}/load``), ``loading`` (a load
    #: is in progress), ``failed`` (a load attempt failed; see the reason) or
    #: ``unavailable`` (misconfigured / missing stack; see the reason).
    load_state: str = "loaded"


@dataclass(frozen=True)
class PointPrompt:
    x: float
    y: float
    positive: bool


@dataclass
class SegmentationProposal:
    #: Boolean (H, W) array aligned exactly with the source image grid.
    mask: np.ndarray
    score: float | None
    backend_metadata: dict[str, object] = field(default_factory=dict)


class BackendCapabilityError(Exception):
    """Raised when a backend does not support a requested prompt kind."""

    code = "MODEL_CAPABILITY_UNSUPPORTED"

    def __init__(self, backend_id: str, capability: BackendCapability, reason: str) -> None:
        self.backend_id = backend_id
        self.capability = capability
        self.reason = reason
        super().__init__(f"{backend_id}: unsupported capability {capability.value}: {reason}")


class BackendUnavailableError(Exception):
    """Raised by every inference call on an unavailable backend."""

    code = "MODEL_UNAVAILABLE"

    def __init__(self, backend_id: str, reason: str) -> None:
        self.backend_id = backend_id
        self.reason = reason
        super().__init__(f"{backend_id}: unavailable: {reason}")


class BackendOutOfMemoryError(Exception):
    """Raised when inference fails because of host or device memory exhaustion."""

    code = "MODEL_OOM"

    def __init__(self, backend_id: str, reason: str) -> None:
        self.backend_id = backend_id
        self.reason = reason
        super().__init__(f"{backend_id}: out of memory: {reason}")


@runtime_checkable
class SegmentationBackend(Protocol):
    backend_id: str

    def descriptor(self) -> BackendDescriptor: ...

    def segment(
        self,
        image: np.ndarray,
        *,
        points: list[PointPrompt],
        box_xyxy: tuple[float, float, float, float] | None,
        previous_mask: np.ndarray | None,
        text_prompt: str | None,
    ) -> list[SegmentationProposal]: ...

    def release(self) -> None:
        """Free model/session resources. Must be safe to call repeatedly."""
        ...


def validate_prompts_against_capabilities(
    descriptor: BackendDescriptor,
    *,
    points: list[PointPrompt],
    box_xyxy: tuple[float, float, float, float] | None,
    previous_mask: np.ndarray | None,
    text_prompt: str | None,
) -> None:
    """Shared capability gate every backend can call before inference."""
    capabilities = descriptor.capabilities
    backend_id = descriptor.backend_id
    if points and BackendCapability.POINT_PROMPTS not in capabilities:
        raise BackendCapabilityError(
            backend_id, BackendCapability.POINT_PROMPTS, "point prompts are not supported"
        )
    if box_xyxy is not None and BackendCapability.BOX_PROMPTS not in capabilities:
        raise BackendCapabilityError(
            backend_id, BackendCapability.BOX_PROMPTS, "box prompts are not supported"
        )
    if previous_mask is not None and BackendCapability.MASK_PROMPTS not in capabilities:
        raise BackendCapabilityError(
            backend_id, BackendCapability.MASK_PROMPTS, "mask prompts are not supported"
        )
    if text_prompt is not None and BackendCapability.TEXT_PROMPTS not in capabilities:
        raise BackendCapabilityError(
            backend_id, BackendCapability.TEXT_PROMPTS, "text prompts are not supported"
        )
    if not points and box_xyxy is None and previous_mask is None and text_prompt is None:
        raise ValueError("at least one prompt (point, box, mask or text) is required")


def image_height_width(image: np.ndarray) -> tuple[int, int]:
    if image.ndim not in (2, 3):
        raise ValueError(f"expected a (H, W) or (H, W, C) image, got shape {image.shape}")
    return int(image.shape[0]), int(image.shape[1])
