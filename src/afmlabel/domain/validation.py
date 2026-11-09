# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Overlap findings and validation report models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from afmlabel.domain.common import VALIDATION_SCHEMA_VERSION
from afmlabel.domain.region import OverlapReviewState


class OverlapGroup(BaseModel):
    group_id: str
    pixel_count: int
    max_multiplicity: int
    bbox_xywh: list[int]
    region_ids: list[str]
    class_ids: list[int]
    review_state: OverlapReviewState = "unreviewed"
    reviewed_at: str | None = None
    note: str | None = None
    #: Which region currently wins (highest z among participants).
    winning_region_id: str | None = None


class MissingComponent(BaseModel):
    pixel_count: int
    bbox_xywh: list[int]


class CoverageSummary(BaseModel):
    total_pixels: int
    labeled_pixels: int
    unlabeled_pixels: int
    coverage_percent: float
    overlapped_pixels: int
    max_multiplicity: int


class OverlapReport(BaseModel):
    dataset_id: str
    dataset_revision: int
    coverage: CoverageSummary
    groups: list[OverlapGroup] = Field(default_factory=list)
    unreviewed_count: int = 0
    missing_components: list[MissingComponent] = Field(default_factory=list)
    missing_component_count: int = 0
    hidden_region_ids: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    code: str
    message: str
    region_ids: list[str] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    group_ids: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


ValidationStatus = Literal["valid", "blocked"]


class ValidationReport(BaseModel):
    schema_version: int = VALIDATION_SCHEMA_VERSION
    dataset_id: str
    dataset_revision: int
    project_revision: int
    computed_at: str
    status: ValidationStatus
    blocking: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    coverage: CoverageSummary
    overlap_group_count: int
    unreviewed_overlap_count: int
    class_pixel_histogram: dict[int, int] = Field(default_factory=dict)
    region_count: int
    boundary_mode: str
    require_overlap_review: bool
    #: SHA-256 of the composed label map (int32 little-endian), for determinism checks.
    composition_sha256: str | None = None
