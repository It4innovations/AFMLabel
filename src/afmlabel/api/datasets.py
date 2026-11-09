# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Multi-channel image import batches, datasets, images and source replacement."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from afmlabel.api.deps import get_actor, get_store
from afmlabel.api.projects import require_editing_rights
from afmlabel.domain.dataset import Dataset, DatasetSummary
from afmlabel.domain.project import ChannelKey, Project
from afmlabel.errors import AppError
from afmlabel.imports.batches import (
    BatchFileRecord,
    BatchStore,
    PairingPreviewResponse,
    UploadBatch,
)
from afmlabel.persistence import paths
from afmlabel.projects.datasets import ChannelNotFoundError, DatasetStore
from afmlabel.projects.sanitize import safe_filename, sanitize_display_name
from afmlabel.projects.store import ProjectStore

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["datasets"])

#: Path/query parameter naming one project channel (``topography`` ...).
ChannelParam = ChannelKey


def get_datasets(request: Request) -> DatasetStore:
    store: DatasetStore = request.app.state.dataset_store
    return store


def get_batches(request: Request) -> BatchStore:
    store: BatchStore = request.app.state.batch_store
    return store


def require_project_channel(project: Project, channel: str) -> ChannelKey:
    if project.channel(channel) is None:
        raise ChannelNotFoundError(project.project_id, channel)
    return channel


class BatchCreatedResponse(BaseModel):
    batch_id: str


class PairSelection(BaseModel):
    pairing_key: str
    #: ``{channel_key: file_id}`` with one entry per project channel.
    files: dict[ChannelKey, str] = Field(default_factory=dict)
    #: Legacy two-channel spelling, still accepted.
    topography_file_id: str | None = None
    friction_file_id: str | None = None

    def selection(self) -> dict[ChannelKey, str]:
        chosen = dict(self.files)
        if self.topography_file_id:
            chosen.setdefault("topography", self.topography_file_id)
        if self.friction_file_id:
            chosen.setdefault("friction", self.friction_file_id)
        return chosen


class CommitRequest(BaseModel):
    expected_revision: int
    pairs: list[PairSelection]
    #: Must be true when the preview was produced with order-based pairing.
    confirm_order_based: bool = False


class CommitResponse(BaseModel):
    datasets: list[DatasetSummary]
    project_revision: int


class DatasetDetail(BaseModel):
    dataset: Dataset
    previous_dataset_id: str | None
    next_dataset_id: str | None
    region_count: int


class DatasetPatchRequest(BaseModel):
    expected_revision: int
    name: str | None = None


class ReplacementReport(BaseModel):
    replacement_id: str
    compatible: bool
    code: str | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    current: dict[str, Any]
    candidate: dict[str, Any]


class ReplacementConfirm(BaseModel):
    replacement_id: str
    expected_revision: int


# ---------------------------------------------------------------- batches


@router.post("/imports/pairs/batches", status_code=201)
def create_batch(
    project_id: str,
    request: Request,
    store: ProjectStore = Depends(get_store),
    batches: BatchStore = Depends(get_batches),
) -> BatchCreatedResponse:
    store.get_project(project_id)
    require_editing_rights(request, project_id)
    return BatchCreatedResponse(batch_id=batches.create(project_id).batch_id)


@router.post("/imports/pairs/batches/{batch_id}/files")
def upload_batch_files(
    project_id: str,
    batch_id: str,
    channel: str | None = Query(default=None),
    modality: str | None = Query(default=None, description="legacy alias of channel"),
    files: list[UploadFile] = File(...),
    store: ProjectStore = Depends(get_store),
    batches: BatchStore = Depends(get_batches),
) -> PairingPreviewResponse:
    project = store.get_project(project_id)
    key = require_project_channel(project, channel or modality or "")
    batch = batches.get(batch_id, project_id)
    for upload in files:
        name = upload.filename or "image"
        try:
            batches.add_file(batch, key, name, upload.file, channels=project.channels)
        finally:
            upload.file.close()
    return batches.preview(batch, channels=project.channels)


@router.get("/imports/pairs/batches/{batch_id}")
def get_batch_preview(
    project_id: str,
    batch_id: str,
    allow_order_based: bool = Query(default=False),
    store: ProjectStore = Depends(get_store),
    batches: BatchStore = Depends(get_batches),
) -> PairingPreviewResponse:
    project = store.get_project(project_id)
    return batches.preview(
        batches.get(batch_id, project_id),
        channels=project.channels,
        allow_order_based=allow_order_based,
    )


@router.delete("/imports/pairs/batches/{batch_id}/files/{file_id}")
def remove_batch_file(
    project_id: str,
    batch_id: str,
    file_id: str,
    store: ProjectStore = Depends(get_store),
    batches: BatchStore = Depends(get_batches),
) -> PairingPreviewResponse:
    project = store.get_project(project_id)
    batch = batches.get(batch_id, project_id)
    batches.remove_file(batch, file_id)
    return batches.preview(batch, channels=project.channels)


@router.delete("/imports/pairs/batches/{batch_id}", status_code=204)
def discard_batch(
    project_id: str, batch_id: str, batches: BatchStore = Depends(get_batches)
) -> Response:
    batches.get(batch_id, project_id)
    batches.discard(batch_id)
    return Response(status_code=204)


@router.post("/imports/pairs/batches/{batch_id}/commit")
def commit_batch(
    project_id: str,
    batch_id: str,
    payload: CommitRequest,
    request: Request,
    batches: BatchStore = Depends(get_batches),
    datasets: DatasetStore = Depends(get_datasets),
    store: ProjectStore = Depends(get_store),
) -> CommitResponse:
    require_editing_rights(request, project_id)
    project = store.get_project(project_id)
    batch: UploadBatch = batches.get(batch_id, project_id)
    preview = batches.raw_preview(
        batch, channels=project.channels, allow_order_based=payload.confirm_order_based
    )
    ok_rows = {
        (row.pairing_key, tuple(sorted(row.file_ids().items()))): row
        for row in preview.rows
        if row.status == "ok"
    }
    if not payload.pairs:
        raise AppError(
            "PAIR_MISSING_IMAGE",
            "no pairs selected",
            status_code=422,
            target={"batch_id": batch_id},
        )
    selections: list[tuple[str, dict[ChannelKey, str]]] = []
    for selection in payload.pairs:
        chosen = selection.selection()
        key = (selection.pairing_key, tuple(sorted(chosen.items())))
        if key not in ok_rows:
            matching = [r for r in preview.rows if r.pairing_key == selection.pairing_key]
            status = matching[0].status if matching else "unknown"
            code = {
                "ambiguous": "PAIR_AMBIGUOUS",
                "dimension_mismatch": "PAIR_DIMENSION_MISMATCH",
                "calibration_mismatch": "PAIR_REGISTRATION_MISMATCH",
            }.get(status, "PAIR_MISSING_IMAGE")
            raise AppError(
                code,
                f"pair {selection.pairing_key!r} is not committable (status: {status})",
                status_code=422,
                target={"batch_id": batch_id, "pairing_key": selection.pairing_key},
                recovery="resolve the pairing problem in the preview before committing",
            )
        selections.append((selection.pairing_key, chosen))
    created = datasets.commit_batch(
        project_id,
        batch,
        selections,
        pairing_method=preview.method,
        expected_revision=payload.expected_revision,
        actor=get_actor(request),
    )
    batches.discard(batch_id)
    return CommitResponse(
        datasets=[datasets.summary(d) for d in created],
        project_revision=store.get_project(project_id).revision,
    )


# ---------------------------------------------------------------- datasets


def dataset_matches(summary: DatasetSummary, query: str) -> bool:
    """Case-insensitive substring match on metadata only (no image decode):
    original filenames, pairing key, display name and dataset id."""
    needle = query.strip().casefold()
    if not needle:
        return True
    haystack = (
        *(s.original_filename for s in summary.sources),
        summary.pairing_key,
        summary.name,
        summary.dataset_id,
    )
    return any(needle in value.casefold() for value in haystack)


@router.get("/datasets")
def list_datasets(
    project_id: str,
    q: str | None = Query(default=None, max_length=200),
    datasets: DatasetStore = Depends(get_datasets),
) -> list[DatasetSummary]:
    summaries = datasets.list_datasets(project_id)
    if q:
        summaries = [s for s in summaries if dataset_matches(s, q)]
    return summaries


@router.get("/datasets/{dataset_id}")
def get_dataset(
    project_id: str, dataset_id: str, datasets: DatasetStore = Depends(get_datasets)
) -> DatasetDetail:
    dataset = datasets.get(project_id, dataset_id)
    previous, following = datasets.neighbours(project_id, dataset_id)
    return DatasetDetail(
        dataset=dataset,
        previous_dataset_id=previous,
        next_dataset_id=following,
        region_count=datasets.region_counter(datasets.dataset_dir(project_id, dataset_id)),
    )


@router.patch("/datasets/{dataset_id}")
def patch_dataset(
    project_id: str,
    dataset_id: str,
    payload: DatasetPatchRequest,
    request: Request,
    datasets: DatasetStore = Depends(get_datasets),
) -> Dataset:
    require_editing_rights(request, project_id)

    def mutate(dataset: Dataset) -> None:
        if payload.name is not None:
            dataset.name = sanitize_display_name(payload.name, what="dataset name")

    return datasets.mutate(
        project_id,
        dataset_id,
        payload.expected_revision,
        mutate,
        operation="dataset.update",
        actor=get_actor(request),
        invalidates_validation=False,
        details={"name": payload.name},
    )


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(
    project_id: str,
    dataset_id: str,
    request: Request,
    expected_revision: int | None = Query(default=None),
    datasets: DatasetStore = Depends(get_datasets),
) -> Response:
    require_editing_rights(request, project_id)
    datasets.delete(project_id, dataset_id, expected_revision, actor=get_actor(request))
    return Response(status_code=204)


@router.get("/datasets/{dataset_id}/image/{channel}")
def dataset_display_image(
    project_id: str,
    dataset_id: str,
    channel: ChannelParam,
    datasets: DatasetStore = Depends(get_datasets),
) -> FileResponse:
    dataset = datasets.get(project_id, dataset_id)
    source = datasets.require_source(dataset, channel)
    path = datasets.ensure_display_cache(dataset, channel)
    return FileResponse(
        path,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=0, must-revalidate",
            "ETag": source.sha256[:16],
        },
    )


@router.get("/datasets/{dataset_id}/thumbnail/{channel}")
def dataset_thumbnail(
    project_id: str,
    dataset_id: str,
    channel: ChannelParam,
    datasets: DatasetStore = Depends(get_datasets),
) -> FileResponse:
    dataset = datasets.get(project_id, dataset_id)
    datasets.ensure_display_cache(dataset, channel)
    return FileResponse(
        datasets.thumbnail_path(project_id, dataset_id, channel), media_type="image/png"
    )


@router.get("/datasets/{dataset_id}/source/{channel}")
def dataset_source_file(
    project_id: str,
    dataset_id: str,
    channel: ChannelParam,
    datasets: DatasetStore = Depends(get_datasets),
) -> FileResponse:
    dataset = datasets.get(project_id, dataset_id)
    source = datasets.require_source(dataset, channel)
    return FileResponse(
        datasets.source_path(dataset, channel),
        media_type="application/octet-stream",
        filename=safe_filename(source.original_filename),
    )


# ------------------------------------------------------- source replacement


def _replacement_dir(request: Request, project_id: str, dataset_id: str) -> Path:
    root = paths.uploads_tmp_dir(request.app.state.settings.workspace.root) / "replacements"
    return root / project_id / dataset_id


@router.post("/datasets/{dataset_id}/replace-source/{channel}")
def stage_source_replacement(
    project_id: str,
    dataset_id: str,
    channel: ChannelParam,
    request: Request,
    file: UploadFile = File(...),
    datasets: DatasetStore = Depends(get_datasets),
    batches: BatchStore = Depends(get_batches),
) -> ReplacementReport:
    """Upload a candidate, return the compatibility report; nothing changes yet."""
    require_editing_rights(request, project_id)
    dataset = datasets.get(project_id, dataset_id)
    datasets.require_source(dataset, channel)
    original = file.filename or f"{channel}.tif"
    suffix = Path(safe_filename(original)).suffix.lower()
    if suffix not in (".tif", ".tiff", ".png", ".jpg", ".jpeg"):
        raise AppError(
            "UPLOAD_INVALID",
            f"unsupported file type {suffix!r}",
            status_code=422,
            target={"filename": original},
        )
    from afmlabel.domain.common import new_id

    replacement_id = new_id("repl", 8)
    directory = _replacement_dir(request, project_id, dataset_id) / replacement_id
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"candidate{suffix}"
    part = directory / f"candidate{suffix}.part"
    max_size = int(request.app.state.settings.upload.maximum_file_size_mb * 1024 * 1024)
    size = 0
    try:
        with part.open("wb") as handle:
            while True:
                chunk = file.file.read(512 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > max_size:
                    raise AppError(
                        "UPLOAD_TOO_LARGE",
                        "replacement exceeds the maximum upload size",
                        status_code=413,
                    )
                handle.write(chunk)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    finally:
        file.file.close()
    part.replace(candidate)
    (directory / "original_filename.txt").write_text(original, encoding="utf-8")
    report = datasets.replacement_report(dataset, channel, candidate, original)
    return ReplacementReport(replacement_id=replacement_id, **report)


@router.post("/datasets/{dataset_id}/replace-source/{channel}/confirm")
def confirm_source_replacement(
    project_id: str,
    dataset_id: str,
    channel: ChannelParam,
    payload: ReplacementConfirm,
    request: Request,
    datasets: DatasetStore = Depends(get_datasets),
) -> Dataset:
    require_editing_rights(request, project_id)
    if not payload.replacement_id.startswith("repl_") or "/" in payload.replacement_id:
        raise AppError("UPLOAD_INVALID", "invalid replacement id", status_code=422)
    directory = _replacement_dir(request, project_id, dataset_id) / payload.replacement_id
    candidates = (
        [p for p in directory.glob("candidate.*") if p.is_file()] if directory.exists() else []
    )
    if not candidates:
        raise AppError(
            "UPLOAD_INVALID",
            "staged replacement not found; upload it again",
            status_code=404,
            target={"replacement_id": payload.replacement_id},
        )
    original = (directory / "original_filename.txt").read_text(encoding="utf-8")
    try:
        return datasets.apply_replacement(
            project_id,
            dataset_id,
            channel,
            candidates[0],
            original,
            expected_revision=payload.expected_revision,
            actor=get_actor(request),
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)


__all__ = ["router", "BatchFileRecord"]
