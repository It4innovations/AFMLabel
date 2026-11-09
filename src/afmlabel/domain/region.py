# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Annotation regions and the per-dataset region set."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from afmlabel.domain.common import REGIONS_SCHEMA_VERSION

RegionSource = Literal[
    "manual", "sam_proposal", "tswat_import", "lossy_import", "boundary_fill", "split", "merge"
]

OverlapReviewState = Literal[
    "unreviewed", "reviewed_resolved", "reviewed_intentional", "reviewed_fixed_by_edit"
]


class RleMask(BaseModel):
    """COCO-compatible compressed RLE (``size`` is ``[height, width]``)."""

    size: list[int]
    counts: str


class Region(BaseModel):
    region_id: str
    dataset_id: str
    class_id: int
    #: Higher wins during composition. Unique per dataset.
    z_order: int
    visible: bool = True
    locked: bool = False
    name: str | None = None
    coverage_rle: RleMask
    bbox_xywh: list[int] = Field(default_factory=lambda: [0, 0, 0, 0])
    area: int = 0
    source: RegionSource = "manual"
    #: Set when a model or import produced the mask and a person later edited it.
    user_corrected: bool = False
    revision: int = 1
    created_at: str
    modified_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)


class OverlapReview(BaseModel):
    state: OverlapReviewState = "unreviewed"
    reviewed_at: str | None = None
    note: str | None = None
    #: Participants at review time, for the audit trail.
    region_ids: list[str] = Field(default_factory=list)


class RegionSet(BaseModel):
    schema_version: int = REGIONS_SCHEMA_VERSION
    dataset_id: str
    regions: list[Region] = Field(default_factory=list)
    #: Keyed by overlap group id.
    overlap_reviews: dict[str, OverlapReview] = Field(default_factory=dict)
    next_region_seq: int = 1

    def by_id(self, region_id: str) -> Region | None:
        for region in self.regions:
            if region.region_id == region_id:
                return region
        return None

    def sorted_by_z(self) -> list[Region]:
        return sorted(self.regions, key=lambda r: r.z_order)

    def next_z_order(self) -> int:
        return max((r.z_order for r in self.regions), default=0) + 1

    def renumber_z(self) -> None:
        for index, region in enumerate(self.sorted_by_z(), start=1):
            region.z_order = index
