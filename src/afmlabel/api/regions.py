# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Region endpoints with browser-friendly mask transport."""

from __future__ import annotations

from typing import Any, Literal

import numpy as np
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from afmlabel.annotations.service import RegionService
from afmlabel.api.deps import get_actor, get_store
from afmlabel.api.projects import require_editing_rights
from afmlabel.domain.dataset import Dataset
from afmlabel.domain.region import Region, RegionSet, RegionSource
from afmlabel.errors import AppError
from afmlabel.persistence.rle import decode_coco, decode_rows, encode_rows, polygon_to_mask
from afmlabel.projects.store import ProjectStore

router = APIRouter(prefix="/api/v1/projects/{project_id}/datasets/{dataset_id}", tags=["regions"])


def get_regions(request: Request) -> RegionService:
    service: RegionService = request.app.state.region_service
    return service


class MaskPayload(BaseModel):
    """Either row-major run lengths, COCO RLE or a polygon (image pixels)."""

    format: Literal["rle_rows", "coco_rle", "polygon"]
    size: list[int] | None = None
    counts: list[int] | str | None = None
    points: list[list[float]] | None = None

    def to_array(self, dataset: Dataset) -> np.ndarray:
        height, width = dataset.height, dataset.width
        if self.format == "polygon":
            if not self.points:
                raise AppError("VALIDATION_ERROR", "polygon needs points", status_code=422)
            try:
                return polygon_to_mask([(p[0], p[1]) for p in self.points], height, width)
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", str(exc), status_code=422) from exc
        if self.size is not None and list(self.size) != [height, width]:
            raise AppError(
                "VALIDATION_ERROR",
                f"mask size {self.size} does not match dataset [{height}, {width}]",
                status_code=422,
            )
        if self.format == "rle_rows":
            if not isinstance(self.counts, list):
                raise AppError("VALIDATION_ERROR", "rle_rows needs integer counts", status_code=422)
            try:
                return decode_rows(self.counts, height, width)
            except ValueError as exc:
                raise AppError("VALIDATION_ERROR", str(exc), status_code=422) from exc
        if not isinstance(self.counts, str):
            raise AppError("VALIDATION_ERROR", "coco_rle needs string counts", status_code=422)
        from afmlabel.domain.region import RleMask

        return decode_coco(RleMask(size=[height, width], counts=self.counts))


class RegionOut(BaseModel):
    region_id: str
    class_id: int
    z_order: int
    visible: bool
    locked: bool
    name: str | None
    bbox_xywh: list[int]
    area: int
    source: RegionSource
    user_corrected: bool
    revision: int
    created_at: str
    modified_at: str
    provenance: dict[str, Any]
    mask: dict[str, Any]

    @classmethod
    def from_region(cls, region: Region) -> RegionOut:
        mask = decode_coco(region.coverage_rle)
        return cls(
            region_id=region.region_id,
            class_id=region.class_id,
            z_order=region.z_order,
            visible=region.visible,
            locked=region.locked,
            name=region.name,
            bbox_xywh=region.bbox_xywh,
            area=region.area,
            source=region.source,
            user_corrected=region.user_corrected,
            revision=region.revision,
            created_at=region.created_at,
            modified_at=region.modified_at,
            provenance=region.provenance,
            mask={
                "format": "rle_rows",
                "size": list(region.coverage_rle.size),
                "counts": encode_rows(mask),
            },
        )


class RegionSetOut(BaseModel):
    dataset_id: str
    dataset_revision: int
    validated_revision: int | None
    width: int
    height: int
    regions: list[RegionOut]
    overlap_reviews: dict[str, Any]
    #: Next id the server would assign; echoed back by undo snapshots.
    next_region_seq: int = 1


def region_set_out(dataset: Dataset, region_set: RegionSet) -> RegionSetOut:
    return RegionSetOut(
        dataset_id=dataset.dataset_id,
        dataset_revision=dataset.revision,
        validated_revision=dataset.validated_revision,
        width=dataset.width,
        height=dataset.height,
        regions=[RegionOut.from_region(r) for r in region_set.sorted_by_z()],
        overlap_reviews={k: v.model_dump() for k, v in region_set.overlap_reviews.items()},
        next_region_seq=region_set.next_region_seq,
    )


class RegionMutationOut(BaseModel):
    dataset_revision: int
    region: RegionOut | None = None
    regions: list[RegionOut] = Field(default_factory=list)


class RegionCreateRequest(BaseModel):
    expected_revision: int | None = None
    class_id: int
    mask: MaskPayload
    name: str | None = None
    source: Literal["manual", "sam_proposal"] = "manual"
    provenance: dict[str, Any] = Field(default_factory=dict)


class BulkRegionItem(BaseModel):
    mask: MaskPayload
    name: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class BulkCreateRequest(BaseModel):
    """Several regions of one class in a single revisioned transaction
    (seed-fill tool: *Fill all enclosed gaps*)."""

    expected_revision: int | None = None
    class_id: int
    items: list[BulkRegionItem] = Field(min_length=1, max_length=5000)
    #: Recorded in the audit log, e.g. ``fill_gaps``.
    operation: Literal["fill_gaps", "bulk_create"] = "bulk_create"


class BulkCreateOut(BaseModel):
    dataset_revision: int
    regions: list[RegionOut]


class RegionPatchRequest(BaseModel):
    expected_revision: int | None = None
    class_id: int | None = None
    visible: bool | None = None
    locked: bool | None = None
    name: str | None = None
    clear_name: bool = False
    mask: MaskPayload | None = None


class ReorderRequest(BaseModel):
    expected_revision: int | None = None
    #: Region ids from lowest to highest z-order (bottom to top).
    order: list[str]


class MergeRequest(BaseModel):
    expected_revision: int | None = None
    region_ids: list[str]


class SplitRequest(BaseModel):
    expected_revision: int | None = None


class SubtractRequest(BaseModel):
    expected_revision: int | None = None
    region_ids: list[str]
    #: Regions fully covered by higher selected regions are removed only
    #: when the caller confirmed it (the UI shows the preview first).
    allow_empty_removal: bool = False


class SubtractPlanEntry(BaseModel):
    region_id: str
    class_id: int
    z_order: int
    locked: bool
    pixels_before: int
    pixels_after: int
    pixels_removed: int
    becomes_empty: bool


class SubtractPreviewOut(BaseModel):
    dataset_revision: int
    order_top_to_bottom: list[str]
    entries: list[SubtractPlanEntry]
    empty_region_ids: list[str]
    locked_region_ids: list[str]
    total_pixels_removed: int


class SubtractOut(BaseModel):
    dataset_revision: int
    entries: list[SubtractPlanEntry]
    removed_region_ids: list[str]
    regions: list[RegionOut]


class BulkDeleteRequest(BaseModel):
    expected_revision: int | None = None
    region_ids: list[str]


class BulkDeleteOut(BaseModel):
    dataset_revision: int
    deleted_region_ids: list[str]
    kept_locked_region_ids: list[str]


class SnapshotRegion(BaseModel):
    """One region as captured by the browser before a structural operation."""

    region_id: str
    class_id: int
    z_order: int
    visible: bool = True
    locked: bool = False
    name: str | None = None
    source: RegionSource = "manual"
    user_corrected: bool = False
    revision: int = 1
    created_at: str
    modified_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    mask: MaskPayload


class RestoreRequest(BaseModel):
    expected_revision: int | None = None
    regions: list[SnapshotRegion]
    overlap_reviews: dict[str, Any] = Field(default_factory=dict)
    next_region_seq: int | None = None
    #: ``undo`` / ``redo`` / free text, recorded in the audit log.
    reason: str = "undo"


@router.get("/regions")
def list_regions(
    project_id: str, dataset_id: str, service: RegionService = Depends(get_regions)
) -> RegionSetOut:
    dataset, region_set = service.load(project_id, dataset_id)
    return region_set_out(dataset, region_set)


@router.post("/regions", status_code=201)
def create_region(
    project_id: str,
    dataset_id: str,
    payload: RegionCreateRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
    store: ProjectStore = Depends(get_store),
) -> RegionMutationOut:
    require_editing_rights(request, project_id)
    dataset = service.load(project_id, dataset_id)[0]
    mask = payload.mask.to_array(dataset)
    provenance = dict(payload.provenance)
    if payload.source == "sam_proposal" and "backend_id" not in provenance:
        raise AppError(
            "VALIDATION_ERROR",
            "sam_proposal regions must carry backend provenance",
            status_code=422,
            target={"dataset_id": dataset_id},
        )
    dataset, _, region = service.create_region(
        project_id,
        dataset_id,
        payload.expected_revision,
        ontology=store.get_ontology(project_id),
        class_id=payload.class_id,
        mask=mask,
        source=payload.source,
        provenance=provenance,
        name=payload.name,
        actor=get_actor(request),
        event_source="sam_proposal" if payload.source == "sam_proposal" else "manual",
    )
    return RegionMutationOut(
        dataset_revision=dataset.revision, region=RegionOut.from_region(region)
    )


@router.post("/regions/bulk-create", status_code=201)
def create_regions_bulk(
    project_id: str,
    dataset_id: str,
    payload: BulkCreateRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
    store: ProjectStore = Depends(get_store),
) -> BulkCreateOut:
    require_editing_rights(request, project_id)
    dataset = service.load(project_id, dataset_id)[0]
    items = [
        (payload.class_id, item.mask.to_array(dataset), item.name, dict(item.provenance))
        for item in payload.items
    ]
    dataset, _, created = service.create_regions(
        project_id,
        dataset_id,
        payload.expected_revision,
        ontology=store.get_ontology(project_id),
        items=items,
        operation=f"regions.{payload.operation}",
        actor=get_actor(request),
    )
    return BulkCreateOut(
        dataset_revision=dataset.revision, regions=[RegionOut.from_region(r) for r in created]
    )


@router.patch("/regions/{region_id}")
def patch_region(
    project_id: str,
    dataset_id: str,
    region_id: str,
    payload: RegionPatchRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
    store: ProjectStore = Depends(get_store),
) -> RegionMutationOut:
    require_editing_rights(request, project_id)
    dataset = service.load(project_id, dataset_id)[0]
    mask = payload.mask.to_array(dataset) if payload.mask is not None else None
    dataset, _, region = service.update_region(
        project_id,
        dataset_id,
        region_id,
        payload.expected_revision,
        ontology=store.get_ontology(project_id),
        class_id=payload.class_id,
        visible=payload.visible,
        locked=payload.locked,
        name=payload.name,
        clear_name=payload.clear_name,
        mask=mask,
        actor=get_actor(request),
    )
    return RegionMutationOut(
        dataset_revision=dataset.revision, region=RegionOut.from_region(region)
    )


@router.delete("/regions/{region_id}")
def delete_region(
    project_id: str,
    dataset_id: str,
    region_id: str,
    request: Request,
    expected_revision: int | None = Query(default=None),
    service: RegionService = Depends(get_regions),
) -> RegionMutationOut:
    require_editing_rights(request, project_id)
    dataset, _ = service.delete_region(
        project_id, dataset_id, region_id, expected_revision, actor=get_actor(request)
    )
    return RegionMutationOut(dataset_revision=dataset.revision)


@router.post("/regions/reorder")
def reorder_regions(
    project_id: str,
    dataset_id: str,
    payload: ReorderRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
) -> RegionSetOut:
    require_editing_rights(request, project_id)
    dataset, region_set = service.reorder(
        project_id, dataset_id, payload.expected_revision, payload.order, actor=get_actor(request)
    )
    return region_set_out(dataset, region_set)


@router.post("/regions/{region_id}/split")
def split_region(
    project_id: str,
    dataset_id: str,
    region_id: str,
    request: Request,
    payload: SplitRequest | None = None,
    service: RegionService = Depends(get_regions),
) -> RegionMutationOut:
    require_editing_rights(request, project_id)
    dataset, _, produced = service.split_region(
        project_id,
        dataset_id,
        region_id,
        payload.expected_revision if payload else None,
        actor=get_actor(request),
    )
    return RegionMutationOut(
        dataset_revision=dataset.revision, regions=[RegionOut.from_region(r) for r in produced]
    )


@router.post("/regions/merge")
def merge_regions(
    project_id: str,
    dataset_id: str,
    payload: MergeRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
) -> RegionMutationOut:
    require_editing_rights(request, project_id)
    dataset, _, region = service.merge_regions(
        project_id,
        dataset_id,
        payload.region_ids,
        payload.expected_revision,
        actor=get_actor(request),
    )
    return RegionMutationOut(
        dataset_revision=dataset.revision, region=RegionOut.from_region(region)
    )


@router.post("/regions/subtract-by-order/preview")
def subtract_preview(
    project_id: str,
    dataset_id: str,
    payload: SubtractRequest,
    service: RegionService = Depends(get_regions),
) -> SubtractPreviewOut:
    dataset, region_set = service.load(project_id, dataset_id)
    plan = service.subtract_plan(region_set, payload.region_ids)
    entries = [SubtractPlanEntry(**{k: v for k, v in p.items() if k != "mask"}) for p in plan]
    return SubtractPreviewOut(
        dataset_revision=dataset.revision,
        order_top_to_bottom=[e.region_id for e in entries],
        entries=entries,
        empty_region_ids=[e.region_id for e in entries if e.becomes_empty],
        locked_region_ids=[e.region_id for e in entries if e.locked and e.pixels_removed > 0],
        total_pixels_removed=sum(e.pixels_removed for e in entries),
    )


@router.post("/regions/subtract-by-order")
def subtract_by_order(
    project_id: str,
    dataset_id: str,
    payload: SubtractRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
) -> SubtractOut:
    require_editing_rights(request, project_id)
    dataset, region_set, outcome = service.subtract_by_order(
        project_id,
        dataset_id,
        payload.region_ids,
        payload.expected_revision,
        allow_empty_removal=payload.allow_empty_removal,
        actor=get_actor(request),
    )
    entries = [SubtractPlanEntry(**o) for o in outcome]
    return SubtractOut(
        dataset_revision=dataset.revision,
        entries=entries,
        removed_region_ids=[e.region_id for e in entries if e.becomes_empty],
        regions=[RegionOut.from_region(r) for r in region_set.sorted_by_z()],
    )


@router.post("/regions/bulk-delete")
def delete_regions(
    project_id: str,
    dataset_id: str,
    payload: BulkDeleteRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
) -> BulkDeleteOut:
    require_editing_rights(request, project_id)
    dataset, _, deleted, kept = service.delete_regions(
        project_id,
        dataset_id,
        payload.region_ids,
        payload.expected_revision,
        actor=get_actor(request),
    )
    return BulkDeleteOut(
        dataset_revision=dataset.revision, deleted_region_ids=deleted, kept_locked_region_ids=kept
    )


@router.post("/regions/restore")
def restore_regions(
    project_id: str,
    dataset_id: str,
    payload: RestoreRequest,
    request: Request,
    service: RegionService = Depends(get_regions),
    store: ProjectStore = Depends(get_store),
) -> RegionSetOut:
    """Atomic undo/redo transport for structural operations: the browser
    sends the snapshot it captured, the server validates and persists it as
    one revisioned mutation."""
    require_editing_rights(request, project_id)
    dataset = service.load(project_id, dataset_id)[0]
    from afmlabel.persistence.rle import encode_coco

    regions = [
        Region(
            region_id=item.region_id,
            dataset_id=dataset_id,
            class_id=item.class_id,
            z_order=item.z_order,
            visible=item.visible,
            locked=item.locked,
            name=item.name,
            coverage_rle=encode_coco(item.mask.to_array(dataset)),
            source=item.source,
            user_corrected=item.user_corrected,
            revision=item.revision,
            created_at=item.created_at,
            modified_at=item.modified_at,
            provenance=item.provenance,
        )
        for item in payload.regions
    ]
    dataset, region_set = service.restore_snapshot(
        project_id,
        dataset_id,
        payload.expected_revision,
        ontology=store.get_ontology(project_id),
        regions=regions,
        overlap_reviews=payload.overlap_reviews,
        next_region_seq=payload.next_region_seq,
        reason=payload.reason,
        actor=get_actor(request),
    )
    return region_set_out(dataset, region_set)


@router.post("/regions/{region_id}/duplicate")
def duplicate_region(
    project_id: str,
    dataset_id: str,
    region_id: str,
    request: Request,
    payload: SplitRequest | None = None,
    service: RegionService = Depends(get_regions),
) -> RegionMutationOut:
    require_editing_rights(request, project_id)
    dataset, _, region = service.duplicate_region(
        project_id,
        dataset_id,
        region_id,
        payload.expected_revision if payload else None,
        actor=get_actor(request),
    )
    return RegionMutationOut(
        dataset_revision=dataset.revision, region=RegionOut.from_region(region)
    )
