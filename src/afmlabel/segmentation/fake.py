# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Deterministic, CPU-only test double implementing the full backend contract.

Positive points grow disks, negative points carve disks out, boxes fill a
rectangle and a previous mask seeds the result. Text prompts are rejected
with a typed :class:`BackendCapabilityError` so the contract suite covers
both the supported and the rejected path. Nothing here is a model.
"""

from __future__ import annotations

import numpy as np

from afmlabel import __version__
from afmlabel.segmentation.base import (
    BackendCapability,
    BackendDescriptor,
    PointPrompt,
    SegmentationProposal,
    image_height_width,
    validate_prompts_against_capabilities,
)

_POINT_RADIUS = 6.0


def disk_mask(height: int, width: int, cx: float, cy: float, radius: float) -> np.ndarray:
    yy, xx = np.ogrid[:height, :width]
    result: np.ndarray = ((xx - cx) ** 2 + (yy - cy) ** 2) <= radius**2
    return result


def box_mask(height: int, width: int, box: tuple[float, float, float, float]) -> np.ndarray:
    x0, y0, x1, y1 = box
    mask = np.zeros((height, width), dtype=bool)
    xi0, yi0 = max(0, int(round(min(x0, x1)))), max(0, int(round(min(y0, y1))))
    xi1, yi1 = min(width, int(round(max(x0, x1)))), min(height, int(round(max(y0, y1))))
    mask[yi0:yi1, xi0:xi1] = True
    return mask


class FakeBackend:
    backend_id = "fake"

    def __init__(self, point_radius: float = _POINT_RADIUS) -> None:
        self._point_radius = point_radius
        self.release_count = 0

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_id=self.backend_id,
            display_name="Fake backend (deterministic, CPU-only)",
            capabilities=frozenset(
                {
                    BackendCapability.POINT_PROMPTS,
                    BackendCapability.BOX_PROMPTS,
                    BackendCapability.MASK_PROMPTS,
                    BackendCapability.MULTI_MASK_PROPOSALS,
                }
            ),
            available=True,
            unavailable_reason=None,
            model_repository=None,
            model_revision=None,
            checkpoint_sha256=None,
            code_revision=__version__,
            device="cpu",
            dtype="bool",
            warnings=("Synthetic geometry only -- for development and tests, never science.",),
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
        descriptor = self.descriptor()
        validate_prompts_against_capabilities(
            descriptor,
            points=points,
            box_xyxy=box_xyxy,
            previous_mask=previous_mask,
            text_prompt=text_prompt,
        )
        height, width = image_height_width(image)
        mask = np.zeros((height, width), dtype=bool)
        if previous_mask is not None:
            if previous_mask.shape != (height, width):
                raise ValueError(
                    f"previous_mask shape {previous_mask.shape} does not match image "
                    f"shape {(height, width)}"
                )
            mask |= previous_mask.astype(bool)
        if box_xyxy is not None:
            mask |= box_mask(height, width, box_xyxy)
        for point in points:
            disk = disk_mask(height, width, point.x, point.y, self._point_radius)
            if point.positive:
                mask |= disk
            else:
                mask &= ~disk
        # Two proposals: the primary mask and a slightly larger alternative,
        # exercising MULTI_MASK_PROPOSALS handling in the UI.
        alternative = mask.copy()
        for point in points:
            if point.positive:
                alternative |= disk_mask(height, width, point.x, point.y, self._point_radius * 1.5)
        return [
            SegmentationProposal(mask=mask, score=1.0, backend_metadata={"synthetic": True}),
            SegmentationProposal(
                mask=alternative,
                score=0.75,
                backend_metadata={"synthetic": True, "variant": "dilated"},
            ),
        ]

    def release(self) -> None:
        self.release_count += 1
