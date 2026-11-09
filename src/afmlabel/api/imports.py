# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Legacy import endpoints: TSWAT/CVAT XML and lossy flattened rasters."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from pydantic import BaseModel, Field

from afmlabel.api.deps import get_actor, get_store
from afmlabel.api.projects import require_editing_rights
from afmlabel.domain.common import new_id
from afmlabel.errors import AppError, NotFoundError
from afmlabel.imports.lossy import (
    LossyCommitResult,
    LossyPreview,
    commit_lossy_raster,
    preview_lossy_raster,
)
from afmlabel.imports.tswat import TswatCommitReport, TswatImportService, TswatStageReport
from afmlabel.persistence import paths
from afmlabel.projects.sanitize import safe_filename
from afmlabel.projects.store import ProjectStore

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["imports"])

MAX_XML_BYTES = 64 * 1024 * 1024


def get_tswat(request: Request) -> TswatImportService:
    service: TswatImportService = request.app.state.tswat_service
    return service


class TswatCommitRequest(BaseModel):
    expected_revision: int
    #: label -> class id (as string), "create" or "skip"
    class_mapping: dict[str, str] = Field(default_factory=dict)
    target_dataset_id: str | None = None


class LossyStageResponse(BaseModel):
    staged_id: str
    preview: LossyPreview


class LossyCommitRequest(BaseModel):
    staged_id: str
    expected_revision: int | None = None
    #: raster value -> class id, or ``null`` for "do not import this value".
    #: Every value present in the raster must be listed (nothing is skipped
    #: silently); ``null`` entries additionally require ``confirm_discard``.
    value_to_class: dict[int, int | None]
    split_components: bool = True
    confirm_discard: bool = False


# ------------------------------------------------------------------- tswat


@router.post("/imports/tswat/stage")
def stage_tswat(
    project_id: str,
    request: Request,
    file: UploadFile = File(...),
    target_dataset_id: str | None = Form(default=None),
    store: ProjectStore = Depends(get_store),
    service: TswatImportService = Depends(get_tswat),
) -> TswatStageReport:
    require_editing_rights(request, project_id)
    store.get_project(project_id)
    data = file.file.read(MAX_XML_BYTES + 1)
    file.file.close()
    if len(data) > MAX_XML_BYTES:
        raise AppError("UPLOAD_TOO_LARGE", "XML file exceeds 64 MB", status_code=413)
    staged, parsed = service.stage(project_id, file.filename or "annotations.xml", data)
    return service.build_report(project_id, staged, parsed, target_dataset_id=target_dataset_id)


@router.get("/imports/tswat/{staged_id}")
def get_tswat_report(
    project_id: str,
    staged_id: str,
    target_dataset_id: str | None = None,
    class_mapping: str | None = None,
    service: TswatImportService = Depends(get_tswat),
) -> TswatStageReport:
    staged, parsed = service.load_staged(project_id, staged_id)
    mapping = json.loads(class_mapping) if class_mapping else None
    return service.build_report(
        project_id, staged, parsed, target_dataset_id=target_dataset_id, class_mapping=mapping
    )


@router.post("/imports/tswat/{staged_id}/commit")
def commit_tswat(
    project_id: str,
    staged_id: str,
    payload: TswatCommitRequest,
    request: Request,
    service: TswatImportService = Depends(get_tswat),
) -> TswatCommitReport:
    require_editing_rights(request, project_id)
    return service.commit(
        project_id,
        staged_id,
        expected_project_revision=payload.expected_revision,
        class_mapping=payload.class_mapping,
        target_dataset_id=payload.target_dataset_id,
        actor=get_actor(request),
    )


@router.delete("/imports/tswat/{staged_id}", status_code=204)
def discard_tswat(
    project_id: str, staged_id: str, service: TswatImportService = Depends(get_tswat)
) -> Response:
    service.load_staged(project_id, staged_id)
    service.discard(staged_id)
    return Response(status_code=204)


# ------------------------------------------------------------------- lossy


def _lossy_dir(request: Request, project_id: str, dataset_id: str, staged_id: str) -> Path:
    if (
        not staged_id.startswith("lossy_")
        or "/" in staged_id
        or "\\" in staged_id
        or ".." in staged_id
    ):
        raise NotFoundError("TSWAT_IMPORT_INVALID", f"unknown staged raster {staged_id}")
    root = paths.uploads_tmp_dir(request.app.state.settings.workspace.root) / "lossy"
    return root / project_id / dataset_id / staged_id


@router.post("/datasets/{dataset_id}/imports/lossy-mask/stage")
def stage_lossy(
    project_id: str,
    dataset_id: str,
    request: Request,
    file: UploadFile = File(...),
) -> LossyStageResponse:
    require_editing_rights(request, project_id)
    dataset = request.app.state.dataset_store.get(project_id, dataset_id)
    original = file.filename or "mask.png"
    suffix = Path(safe_filename(original)).suffix.lower()
    if suffix not in (".png", ".tif", ".tiff"):
        raise AppError(
            "UPLOAD_INVALID", f"unsupported label raster type {suffix!r}", status_code=422
        )
    staged_id = new_id("lossy", 8)
    directory = _lossy_dir(request, project_id, dataset_id, staged_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"raster{suffix}"
    max_size = int(request.app.state.settings.upload.maximum_file_size_mb * 1024 * 1024)
    size = 0
    with target.open("wb") as handle:
        while True:
            chunk = file.file.read(512 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_size:
                handle.close()
                shutil.rmtree(directory, ignore_errors=True)
                raise AppError(
                    "UPLOAD_TOO_LARGE", "raster exceeds the maximum upload size", status_code=413
                )
            handle.write(chunk)
    file.file.close()
    (directory / "original_filename.txt").write_text(original, encoding="utf-8")
    store: ProjectStore = request.app.state.project_store
    preview, _ = preview_lossy_raster(
        target,
        original,
        dataset,
        store.get_ontology(project_id),
        store.get_project(project_id).config,
    )
    return LossyStageResponse(staged_id=staged_id, preview=preview)


@router.post("/datasets/{dataset_id}/imports/lossy-mask/commit")
def commit_lossy(
    project_id: str,
    dataset_id: str,
    payload: LossyCommitRequest,
    request: Request,
    store: ProjectStore = Depends(get_store),
) -> LossyCommitResult:
    require_editing_rights(request, project_id)
    dataset = request.app.state.dataset_store.get(project_id, dataset_id)
    directory = _lossy_dir(request, project_id, dataset_id, payload.staged_id)
    rasters = [p for p in directory.glob("raster.*") if p.is_file()] if directory.exists() else []
    if not rasters:
        raise NotFoundError(
            "TSWAT_IMPORT_INVALID",
            "staged raster not found; upload it again",
            target={"staged_id": payload.staged_id},
        )
    original = (directory / "original_filename.txt").read_text(encoding="utf-8")
    preview, labels = preview_lossy_raster(rasters[0], original, dataset)
    if not preview.compatible or labels is None:
        raise AppError(
            "TSWAT_IMPORT_INVALID",
            preview.reason or "raster is not importable",
            status_code=422,
            target={"dataset_id": dataset_id},
        )
    result = commit_lossy_raster(
        request.app.state.region_service,
        project_id,
        dataset,
        labels,
        sha256=preview.sha256,
        filename=original,
        value_to_class=payload.value_to_class,
        split=payload.split_components,
        confirm_discard=payload.confirm_discard,
        expected_revision=payload.expected_revision,
        actor=get_actor(request),
        ontology_class_ids={c.class_id for c in store.get_ontology(project_id).classes},
    )
    # The staged raster is removed only after a successful commit so a
    # refused mapping can be corrected and committed again.
    shutil.rmtree(directory, ignore_errors=True)
    return result
