# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Segmentation job endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from afmlabel.api.deps import get_actor
from afmlabel.api.projects import require_editing_rights
from afmlabel.domain.jobs import JobRecord, SegmentRequest
from afmlabel.errors import AppError
from afmlabel.jobs.manager import JobManager

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/datasets/{dataset_id}", tags=["segmentation"]
)


def get_jobs(request: Request) -> JobManager:
    manager: JobManager = request.app.state.job_manager
    return manager


class JobAccepted(BaseModel):
    job: JobRecord


@router.post("/segment", status_code=202)
def submit_segmentation(
    project_id: str,
    dataset_id: str,
    payload: SegmentRequest,
    request: Request,
    manager: JobManager = Depends(get_jobs),
) -> JobRecord:
    require_editing_rights(request, project_id)
    if (
        not payload.points
        and payload.box_xyxy is None
        and not payload.previous_region_id
        and payload.text_prompt is None
    ):
        raise AppError(
            "VALIDATION_ERROR",
            "at least one prompt (point, box, previous region or text) is required",
            status_code=422,
        )
    if payload.box_xyxy is not None and len(payload.box_xyxy) != 4:
        raise AppError("VALIDATION_ERROR", "box_xyxy needs four numbers", status_code=422)
    return manager.submit(project_id, dataset_id, payload, actor=get_actor(request)).summary()


@router.get("/jobs")
def list_jobs(
    project_id: str,
    dataset_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    manager: JobManager = Depends(get_jobs),
) -> list[JobRecord]:
    return manager.list_for_dataset(project_id, dataset_id, limit)


@router.get("/jobs/{job_id}")
def get_job(
    project_id: str, dataset_id: str, job_id: str, manager: JobManager = Depends(get_jobs)
) -> JobRecord:
    record = manager.get(job_id)
    if record.project_id != project_id or record.dataset_id != dataset_id:
        raise AppError("JOB_NOT_FOUND", f"job {job_id} belongs to another dataset", status_code=404)
    return record


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    project_id: str,
    dataset_id: str,
    job_id: str,
    request: Request,
    manager: JobManager = Depends(get_jobs),
) -> JobRecord:
    require_editing_rights(request, project_id)
    record = manager.get(job_id)
    if record.project_id != project_id or record.dataset_id != dataset_id:
        raise AppError("JOB_NOT_FOUND", f"job {job_id} belongs to another dataset", status_code=404)
    return manager.cancel(job_id).summary()
