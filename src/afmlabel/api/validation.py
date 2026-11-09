# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Overlap findings, overlap review, validation and Boundary fill endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from afmlabel.api.deps import get_actor, get_store
from afmlabel.api.projects import require_editing_rights
from afmlabel.api.regions import RegionOut
from afmlabel.domain.region import OverlapReviewState
from afmlabel.domain.validation import OverlapReport, ValidationReport
from afmlabel.projects.store import ProjectStore
from afmlabel.validation.findings import FindingsResponse
from afmlabel.validation.service import ValidationService

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/datasets/{dataset_id}", tags=["validation"]
)


def get_validation(request: Request) -> ValidationService:
    service: ValidationService = request.app.state.validation_service
    return service


def policy(request: Request, project_id: str) -> tuple[Any, bool]:
    settings = request.app.state.settings.annotation
    service = get_validation(request)
    return service.policy(project_id, settings.boundary_mode, settings.require_overlap_review)


class ReviewRequest(BaseModel):
    expected_revision: int | None = None
    state: OverlapReviewState
    note: str | None = None


class ReviewResponse(BaseModel):
    dataset_revision: int
    report: OverlapReport


class ValidationStatusResponse(BaseModel):
    report: ValidationReport | None
    stale: bool
    current_revision: int


class BoundaryFillRequest(BaseModel):
    expected_revision: int | None = None
    confirmed_pixel_count: int


class BoundaryFillResponse(BaseModel):
    dataset_revision: int
    region: RegionOut


@router.get("/overlaps")
def overlaps(
    project_id: str, dataset_id: str, service: ValidationService = Depends(get_validation)
) -> OverlapReport:
    return service.overlap_report(project_id, dataset_id)


@router.post("/overlaps/{group_id}/review")
def review_overlap(
    project_id: str,
    dataset_id: str,
    group_id: str,
    payload: ReviewRequest,
    request: Request,
    service: ValidationService = Depends(get_validation),
) -> ReviewResponse:
    require_editing_rights(request, project_id)
    dataset, _, report = service.review_overlap(
        project_id,
        dataset_id,
        group_id,
        payload.expected_revision,
        state=payload.state,
        note=payload.note,
        actor=get_actor(request),
    )
    return ReviewResponse(dataset_revision=dataset.revision, report=report)


class BulkReviewRequest(BaseModel):
    expected_revision: int | None = None
    group_ids: list[str]
    state: OverlapReviewState = "reviewed_resolved"
    note: str | None = None


class BulkReviewResponse(BaseModel):
    dataset_revision: int
    applied_group_ids: list[str]
    missing_group_ids: list[str]
    findings: FindingsResponse


@router.get("/findings")
def list_findings(
    project_id: str,
    dataset_id: str,
    request: Request,
    service: ValidationService = Depends(get_validation),
) -> FindingsResponse:
    """Live Findings computed from the canonical current state (never stale)."""
    boundary_mode, review = policy(request, project_id)
    return service.findings(
        project_id, dataset_id, boundary_mode=boundary_mode, require_overlap_review=review
    )


@router.post("/findings/review")
def review_findings_bulk(
    project_id: str,
    dataset_id: str,
    payload: BulkReviewRequest,
    request: Request,
    service: ValidationService = Depends(get_validation),
) -> BulkReviewResponse:
    """Change the review state of several overlap Findings; geometry is untouched."""
    require_editing_rights(request, project_id)
    dataset, _, applied, missing = service.review_overlaps_bulk(
        project_id,
        dataset_id,
        payload.group_ids,
        payload.expected_revision,
        state=payload.state,
        note=payload.note,
        actor=get_actor(request),
    )
    boundary_mode, review = policy(request, project_id)
    return BulkReviewResponse(
        dataset_revision=dataset.revision,
        applied_group_ids=applied,
        missing_group_ids=missing,
        findings=service.findings(
            project_id, dataset_id, boundary_mode=boundary_mode, require_overlap_review=review
        ),
    )


@router.post("/validate")
def run_validation(
    project_id: str,
    dataset_id: str,
    request: Request,
    service: ValidationService = Depends(get_validation),
) -> ValidationReport:
    boundary_mode, review = policy(request, project_id)
    return service.run_validation(
        project_id,
        dataset_id,
        boundary_mode=boundary_mode,
        require_overlap_review=review,
        actor=get_actor(request),
    )


@router.get("/validation")
def validation_status(
    project_id: str,
    dataset_id: str,
    request: Request,
    service: ValidationService = Depends(get_validation),
    store: ProjectStore = Depends(get_store),
) -> ValidationStatusResponse:
    dataset = request.app.state.dataset_store.get(project_id, dataset_id)
    report = service.latest_report(project_id, dataset_id)
    if report is None:
        return ValidationStatusResponse(report=None, stale=False, current_revision=dataset.revision)
    return ValidationStatusResponse(
        report=report,
        stale=report.dataset_revision != dataset.revision,
        current_revision=dataset.revision,
    )


@router.get("/boundary-fill/preview")
def boundary_fill_preview(
    project_id: str,
    dataset_id: str,
    request: Request,
    service: ValidationService = Depends(get_validation),
) -> dict[str, Any]:
    boundary_mode, _ = policy(request, project_id)
    return service.boundary_fill_preview(project_id, dataset_id, boundary_mode=boundary_mode)


@router.post("/boundary-fill")
def boundary_fill(
    project_id: str,
    dataset_id: str,
    payload: BoundaryFillRequest,
    request: Request,
    service: ValidationService = Depends(get_validation),
) -> BoundaryFillResponse:
    require_editing_rights(request, project_id)
    boundary_mode, _ = policy(request, project_id)
    dataset, _, region = service.boundary_fill(
        project_id,
        dataset_id,
        payload.expected_revision,
        confirmed_pixel_count=payload.confirmed_pixel_count,
        boundary_mode=boundary_mode,
        actor=get_actor(request),
    )
    return BoundaryFillResponse(
        dataset_revision=dataset.revision, region=RegionOut.from_region(region)
    )
