# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Region editing service.

Every operation is a revisioned dataset mutation: ``regions.json`` is
written atomically, then ``dataset.json`` with the bumped revision (which
also clears ``validated_revision``), then an audit event. Regions are never
flattened here; composition belongs to the overlap engine.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from afmlabel.domain.common import utc_now_iso
from afmlabel.domain.dataset import Dataset
from afmlabel.domain.events import EventSource
from afmlabel.domain.project import ClassOntology
from afmlabel.domain.region import Region, RegionSet, RegionSource
from afmlabel.errors import AppError, NotFoundError
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_json, save_model
from afmlabel.persistence.migrations import migrate
from afmlabel.persistence.rle import (
    bounding_box,
    decode_coco,
    encode_coco,
    split_components,
)
from afmlabel.projects.datasets import DatasetStore


class RegionNotFoundError(NotFoundError):
    def __init__(self, dataset_id: str, region_id: str) -> None:
        super().__init__(
            "REGION_REFERENCE_INVALID",
            f"region {region_id} does not exist in dataset {dataset_id}",
            target={"dataset_id": dataset_id, "region_id": region_id},
        )


class RegionLockedError(AppError):
    status_code = 409

    def __init__(self, dataset_id: str, region_id: str) -> None:
        super().__init__(
            "REGION_LOCKED",
            f"region {region_id} is locked",
            target={"dataset_id": dataset_id, "region_id": region_id},
            recovery="unlock the region in the Regions panel first",
        )


def load_region_set(ddir: Path, dataset_id: str) -> RegionSet:
    path = paths.regions_file(ddir)
    if not path.exists():
        return RegionSet(dataset_id=dataset_id)
    return RegionSet.model_validate(migrate("regions", load_json(path)))


def count_regions(ddir: Path) -> int:
    try:
        return len(load_region_set(ddir, ddir.name).regions)
    except (OSError, ValueError):
        return 0


def region_mask(region: Region) -> np.ndarray:
    return decode_coco(region.coverage_rle)


class RegionService:
    def __init__(self, datasets: DatasetStore, *, fsync: bool = True) -> None:
        self._datasets = datasets
        self._fsync = fsync

    # ---------------------------------------------------------------- reads

    def load(self, project_id: str, dataset_id: str) -> tuple[Dataset, RegionSet]:
        dataset = self._datasets.get(project_id, dataset_id)
        ddir = self._datasets.dataset_dir(project_id, dataset_id)
        return dataset, load_region_set(ddir, dataset_id)

    def class_usage(self, project_id: str, class_id: int) -> int:
        """Number of regions across the project that reference ``class_id``."""
        count = 0
        for summary in self._datasets.list_datasets(project_id):
            ddir = self._datasets.dataset_dir(project_id, summary.dataset_id)
            count += sum(
                1
                for r in load_region_set(ddir, summary.dataset_id).regions
                if r.class_id == class_id
            )
        return count

    # -------------------------------------------------------------- mutation

    def mutate(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        fn: Callable[[RegionSet, Dataset], dict[str, Any] | None],
        *,
        operation: str,
        actor: str | None,
        source: EventSource = "manual",
        region_ids: list[str] | None = None,
        class_ids: list[int] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> tuple[Dataset, RegionSet]:
        ddir = self._datasets.dataset_dir(project_id, dataset_id)
        holder: dict[str, Any] = {}
        # Filled in place by ``apply`` so the audit event written by the
        # dataset store (after the mutation) carries the operation details.
        event_details: dict[str, Any] = {}

        def apply(dataset: Dataset) -> None:
            region_set = load_region_set(ddir, dataset_id)
            details = fn(region_set, dataset) or {}
            region_set.renumber_z()
            save_model(paths.regions_file(ddir), region_set, fsync=self._fsync)
            holder["regions"] = region_set
            holder["details"] = details
            event_details.update(details)

        dataset = self._datasets.mutate(
            project_id,
            dataset_id,
            expected_revision,
            apply,
            operation=operation,
            actor=actor,
            source=source,
            details=event_details,
            provenance=provenance,
            region_ids=region_ids,
            class_ids=class_ids,
        )
        return dataset, holder["regions"]

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _check_mask(mask: np.ndarray, dataset: Dataset) -> np.ndarray:
        mask = np.asarray(mask, dtype=bool)
        if mask.shape != (dataset.height, dataset.width):
            raise AppError(
                "VALIDATION_ERROR",
                f"mask shape {mask.shape} does not match dataset {dataset.height}x{dataset.width}",
                status_code=422,
                target={"dataset_id": dataset.dataset_id},
            )
        return mask

    @staticmethod
    def _check_class(ontology: ClassOntology, class_id: int, dataset_id: str) -> None:
        definition = ontology.by_id(class_id)
        if definition is None:
            raise AppError(
                "CLASS_REFERENCE_INVALID",
                f"class {class_id} does not exist in this project",
                status_code=422,
                target={"dataset_id": dataset_id, "class_id": class_id},
                recovery="create the class on the project page first",
            )

    @staticmethod
    def _require(region_set: RegionSet, region_id: str) -> Region:
        region = region_set.by_id(region_id)
        if region is None:
            raise RegionNotFoundError(region_set.dataset_id, region_id)
        return region

    @staticmethod
    def _require_unlocked(region_set: RegionSet, region_id: str) -> Region:
        region = RegionService._require(region_set, region_id)
        if region.locked:
            raise RegionLockedError(region_set.dataset_id, region_id)
        return region

    @staticmethod
    def _set_mask(region: Region, mask: np.ndarray) -> None:
        region.coverage_rle = encode_coco(mask)
        region.bbox_xywh = bounding_box(mask)
        region.area = int(mask.sum())
        region.revision += 1
        region.modified_at = utc_now_iso()

    @staticmethod
    def _new_region(
        region_set: RegionSet,
        dataset: Dataset,
        class_id: int,
        mask: np.ndarray,
        *,
        source: RegionSource,
        provenance: dict[str, Any] | None,
        name: str | None,
        z_order: int | None = None,
    ) -> Region:
        now = utc_now_iso()
        region_id = f"reg_{region_set.next_region_seq:06d}"
        region_set.next_region_seq += 1
        region = Region(
            region_id=region_id,
            dataset_id=dataset.dataset_id,
            class_id=class_id,
            z_order=z_order if z_order is not None else region_set.next_z_order(),
            name=name,
            coverage_rle=encode_coco(mask),
            bbox_xywh=bounding_box(mask),
            area=int(mask.sum()),
            source=source,
            created_at=now,
            modified_at=now,
            provenance=provenance or {},
        )
        region_set.regions.append(region)
        return region

    # ---------------------------------------------------------- operations

    def create_region(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        *,
        ontology: ClassOntology,
        class_id: int,
        mask: np.ndarray,
        source: RegionSource = "manual",
        provenance: dict[str, Any] | None = None,
        name: str | None = None,
        actor: str | None,
        event_source: EventSource = "manual",
    ) -> tuple[Dataset, RegionSet, Region]:
        created: dict[str, Region] = {}

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            self._check_class(ontology, class_id, dataset_id)
            checked = self._check_mask(mask, dataset)
            if not checked.any():
                raise AppError(
                    "VALIDATION_ERROR",
                    "a region must cover at least one pixel",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                )
            region = self._new_region(
                region_set,
                dataset,
                class_id,
                checked,
                source=source,
                provenance=provenance,
                name=name,
            )
            created["region"] = region
            return {"region_id": region.region_id, "area": region.area, "source": source}

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="region.create",
            actor=actor,
            source=event_source,
            class_ids=[class_id],
            provenance=provenance,
        )
        region = created["region"]
        self._datasets._projects.record_event(  # noqa: SLF001 - same package family
            project_id,
            operation="region.created",
            dataset_id=dataset_id,
            region_ids=[region.region_id],
            class_ids=[class_id],
            source=event_source,
            actor=actor,
            provenance=provenance or {},
            details={"area": region.area},
        )
        return dataset, region_set, region

    def create_regions(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        *,
        ontology: ClassOntology,
        items: list[tuple[int, np.ndarray, str | None, dict[str, Any]]],
        source: RegionSource = "manual",
        operation: str = "regions.create_bulk",
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, list[Region]]:
        """Create several regions in one revisioned transaction.

        ``items`` are ``(class_id, mask, name, provenance)`` tuples, e.g. one
        per enclosed gap filled by the seed-fill tool. Either every region is
        created or none is; the regions are appended on top of the z-order
        in the given order.
        """
        created: list[Region] = []

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            if not items:
                raise AppError(
                    "VALIDATION_ERROR",
                    "no regions to create",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                )
            for class_id, mask, name, provenance in items:
                self._check_class(ontology, class_id, dataset_id)
                checked = self._check_mask(mask, dataset)
                if not checked.any():
                    raise AppError(
                        "VALIDATION_ERROR",
                        "a region must cover at least one pixel",
                        status_code=422,
                        target={"dataset_id": dataset_id},
                    )
                created.append(
                    self._new_region(
                        region_set,
                        dataset,
                        class_id,
                        checked,
                        source=source,
                        provenance=provenance,
                        name=name,
                    )
                )
            return {
                "region_ids": [r.region_id for r in created],
                "area": sum(r.area for r in created),
                "source": source,
            }

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation=operation,
            actor=actor,
            class_ids=sorted({class_id for class_id, _, _, _ in items}),
        )
        return dataset, region_set, created

    def update_region(
        self,
        project_id: str,
        dataset_id: str,
        region_id: str,
        expected_revision: int | None,
        *,
        ontology: ClassOntology,
        class_id: int | None = None,
        visible: bool | None = None,
        locked: bool | None = None,
        name: str | None = None,
        clear_name: bool = False,
        mask: np.ndarray | None = None,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, Region]:
        touched: dict[str, Region] = {}

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            region = self._require(region_set, region_id)
            changes: dict[str, Any] = {}
            structural = class_id is not None or mask is not None
            if structural and region.locked and locked is not False:
                raise RegionLockedError(dataset_id, region_id)
            if locked is not None:
                region.locked = locked
                changes["locked"] = locked
            if visible is not None:
                region.visible = visible
                changes["visible"] = visible
            if clear_name:
                region.name = None
            elif name is not None:
                region.name = name.strip()[:100] or None
                changes["name"] = region.name
            if class_id is not None:
                self._check_class(ontology, class_id, dataset_id)
                changes["class_id"] = {"from": region.class_id, "to": class_id}
                region.class_id = class_id
            if mask is not None:
                checked = self._check_mask(mask, dataset)
                if not checked.any():
                    raise AppError(
                        "VALIDATION_ERROR",
                        "a region must keep at least one pixel; delete it instead",
                        status_code=422,
                        target={"dataset_id": dataset_id, "region_id": region_id},
                    )
                self._set_mask(region, checked)
                if region.source != "manual":
                    region.user_corrected = True
                changes["area"] = region.area
            if not (mask is not None) and changes:
                region.revision += 1
                region.modified_at = utc_now_iso()
            touched["region"] = region
            return changes

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="region.update",
            actor=actor,
            region_ids=[region_id],
            class_ids=[class_id] if class_id is not None else None,
        )
        return dataset, region_set, touched["region"]

    def delete_region(
        self,
        project_id: str,
        dataset_id: str,
        region_id: str,
        expected_revision: int | None,
        *,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet]:
        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            region = self._require_unlocked(region_set, region_id)
            region_set.regions = [r for r in region_set.regions if r.region_id != region_id]
            return {"class_id": region.class_id, "area": region.area}

        return self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="region.delete",
            actor=actor,
            region_ids=[region_id],
        )

    def reorder(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        order_bottom_to_top: list[str],
        *,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet]:
        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            ids = [r.region_id for r in region_set.regions]
            if sorted(ids) != sorted(order_bottom_to_top) or len(set(order_bottom_to_top)) != len(
                order_bottom_to_top
            ):
                raise AppError(
                    "REGION_REFERENCE_INVALID",
                    "reorder must list every region exactly once",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                    recovery="reload the region list and try again",
                )
            for index, region_id in enumerate(order_bottom_to_top, start=1):
                self._require(region_set, region_id).z_order = index
            return {"order": order_bottom_to_top}

        return self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="regions.reorder",
            actor=actor,
            region_ids=order_bottom_to_top,
        )

    def split_region(
        self,
        project_id: str,
        dataset_id: str,
        region_id: str,
        expected_revision: int | None,
        *,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, list[Region]]:
        produced: list[Region] = []

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            region = self._require_unlocked(region_set, region_id)
            parts = split_components(region_mask(region))
            if len(parts) < 2:
                raise AppError(
                    "VALIDATION_ERROR",
                    "region has a single connected component; nothing to split",
                    status_code=422,
                    target={"dataset_id": dataset_id, "region_id": region_id},
                )
            self._set_mask(region, parts[0])
            region.source = region.source if region.source == "manual" else region.source
            produced.append(region)
            # New parts sit directly above the original in z-order.
            for offset, part in enumerate(parts[1:], start=1):
                for other in region_set.regions:
                    if other.z_order > region.z_order + offset - 1 and other is not region:
                        other.z_order += 1
                new = self._new_region(
                    region_set,
                    dataset,
                    region.class_id,
                    part,
                    source="split",
                    provenance={"split_from": region_id, "component_index": offset},
                    name=region.name,
                    z_order=region.z_order + offset,
                )
                produced.append(new)
            return {"components": len(parts), "new_region_ids": [r.region_id for r in produced[1:]]}

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="region.split",
            actor=actor,
            region_ids=[region_id],
        )
        return dataset, region_set, produced

    def merge_regions(
        self,
        project_id: str,
        dataset_id: str,
        region_ids: list[str],
        expected_revision: int | None,
        *,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, Region]:
        merged: dict[str, Region] = {}

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            if len(set(region_ids)) < 2:
                raise AppError(
                    "VALIDATION_ERROR",
                    "merge needs at least two distinct regions",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                )
            regions = [self._require_unlocked(region_set, rid) for rid in region_ids]
            classes = {r.class_id for r in regions}
            if len(classes) != 1:
                raise AppError(
                    "CLASS_REFERENCE_INVALID",
                    "only regions of the same class can be merged",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                    recovery="reassign the classes first or merge fewer regions",
                )
            target = regions[0]
            union = region_mask(target)
            for other in regions[1:]:
                union |= region_mask(other)
            self._set_mask(target, union)
            target.provenance = {**target.provenance, "merged_from": region_ids[1:]}
            if target.source != "manual":
                target.user_corrected = True
            drop = set(region_ids[1:])
            region_set.regions = [r for r in region_set.regions if r.region_id not in drop]
            merged["region"] = target
            return {"target": target.region_id, "removed": region_ids[1:]}

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="regions.merge",
            actor=actor,
            region_ids=region_ids,
        )
        return dataset, region_set, merged["region"]

    # ---------------------------------------------- structural bulk operations

    @staticmethod
    def subtract_plan(region_set: RegionSet, region_ids: list[str]) -> list[dict[str, Any]]:
        """Dry run of *Subtract by Order*.

        Selected regions are visited from the highest z-order (top of the
        Regions list) to the lowest. Each region loses every pixel already
        occupied by a selected region above it; non-selected regions are
        never touched. Returns one entry per selected region, top first,
        with the pixel counts before/after and the resulting mask.
        """
        unique = list(dict.fromkeys(region_ids))
        if len(unique) < 2:
            raise AppError(
                "VALIDATION_ERROR",
                "subtract by order needs at least two distinct selected regions",
                status_code=422,
                target={"dataset_id": region_set.dataset_id},
            )
        regions = [RegionService._require(region_set, rid) for rid in unique]
        regions.sort(key=lambda r: r.z_order, reverse=True)
        occupied: np.ndarray | None = None
        plan: list[dict[str, Any]] = []
        for region in regions:
            mask = region_mask(region)
            before = int(mask.sum())
            if occupied is None:
                after_mask = mask
                occupied = mask.copy()
            else:
                after_mask = mask & ~occupied
                occupied |= mask
            after = int(after_mask.sum())
            plan.append(
                {
                    "region_id": region.region_id,
                    "class_id": region.class_id,
                    "z_order": region.z_order,
                    "locked": region.locked,
                    "pixels_before": before,
                    "pixels_after": after,
                    "pixels_removed": before - after,
                    "becomes_empty": after == 0,
                    "mask": after_mask,
                }
            )
        return plan

    def subtract_by_order(
        self,
        project_id: str,
        dataset_id: str,
        region_ids: list[str],
        expected_revision: int | None,
        *,
        allow_empty_removal: bool,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, list[dict[str, Any]]]:
        """Resolve working overlaps between the selected regions geometrically.

        Fully-covered regions are removed only when ``allow_empty_removal``
        is set (the UI asks for confirmation first); locked regions that
        would lose pixels are refused (``REGION_LOCKED``).
        """
        outcome: list[dict[str, Any]] = []

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            plan = self.subtract_plan(region_set, region_ids)
            locked = [p["region_id"] for p in plan if p["locked"] and p["pixels_removed"] > 0]
            if locked:
                raise AppError(
                    "REGION_LOCKED",
                    f"{len(locked)} locked region(s) would lose pixels: {', '.join(locked)}",
                    status_code=409,
                    target={"dataset_id": dataset_id},
                    recovery="unlock the regions or deselect them before subtracting",
                    details={"locked_region_ids": locked},
                )
            empty = [p["region_id"] for p in plan if p["becomes_empty"]]
            if empty and not allow_empty_removal:
                raise AppError(
                    "VALIDATION_ERROR",
                    f"{len(empty)} selected region(s) would become empty after subtraction; "
                    "review the affected regions before confirming",
                    status_code=409,
                    target={"dataset_id": dataset_id},
                    recovery="confirm the removal of the empty regions or change the order",
                    details={"empty_region_ids": empty},
                )
            for entry in plan:
                region = self._require(region_set, entry["region_id"])
                if entry["becomes_empty"]:
                    region_set.regions = [
                        r for r in region_set.regions if r.region_id != region.region_id
                    ]
                elif entry["pixels_removed"] > 0:
                    self._set_mask(region, entry["mask"])
                    if region.source != "manual":
                        region.user_corrected = True
                    region.provenance = {
                        **region.provenance,
                        "subtracted_by_order": {
                            "pixels_removed": entry["pixels_removed"],
                            "at": utc_now_iso(),
                        },
                    }
                outcome.append({k: v for k, v in entry.items() if k != "mask"})
            return {
                "order_top_to_bottom": [p["region_id"] for p in plan],
                "removed_empty": empty,
                "pixels_removed": {p["region_id"]: p["pixels_removed"] for p in plan},
            }

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="regions.subtract_by_order",
            actor=actor,
            region_ids=list(dict.fromkeys(region_ids)),
        )
        return dataset, region_set, outcome

    def delete_regions(
        self,
        project_id: str,
        dataset_id: str,
        region_ids: list[str],
        expected_revision: int | None,
        *,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, list[str], list[str]]:
        """Delete several regions in one revisioned transaction.

        Locked regions are never deleted implicitly: they are reported in
        the second list and stay in place.
        """
        deleted: list[str] = []
        kept_locked: list[str] = []

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            unique = list(dict.fromkeys(region_ids))
            if not unique:
                raise AppError(
                    "VALIDATION_ERROR",
                    "no regions selected",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                )
            for rid in unique:
                region = self._require(region_set, rid)
                if region.locked:
                    kept_locked.append(rid)
                else:
                    deleted.append(rid)
            if not deleted:
                raise RegionLockedError(dataset_id, kept_locked[0])
            drop = set(deleted)
            region_set.regions = [r for r in region_set.regions if r.region_id not in drop]
            return {"deleted": deleted, "kept_locked": kept_locked}

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="regions.delete_selected",
            actor=actor,
            region_ids=list(dict.fromkeys(region_ids)),
        )
        return dataset, region_set, deleted, kept_locked

    def restore_snapshot(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        *,
        ontology: ClassOntology,
        regions: list[Region],
        overlap_reviews: dict[str, Any],
        next_region_seq: int | None,
        reason: str,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet]:
        """Replace the whole region set with a validated snapshot.

        This is the server-side half of undo/redo for structural operations
        (Merge, Split, Subtract by Order, Delete Selected, imports): region
        ids, masks, classes, z-order, visibility, lock state, provenance and
        overlap reviews come back exactly as they were. The snapshot is
        validated (ids, sizes, classes) and applied as one revisioned,
        audited mutation, so the persisted canonical state is never partial.
        """
        from afmlabel.domain.region import OverlapReview

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            ids = [r.region_id for r in regions]
            if len(set(ids)) != len(ids):
                raise AppError(
                    "REGION_REFERENCE_INVALID",
                    "snapshot contains duplicate region ids",
                    status_code=422,
                    target={"dataset_id": dataset_id},
                )
            restored: list[Region] = []
            highest_seq = region_set.next_region_seq
            for region in regions:
                if not region.region_id.startswith("reg_"):
                    raise AppError(
                        "REGION_REFERENCE_INVALID",
                        f"invalid region id {region.region_id!r}",
                        status_code=422,
                        target={"dataset_id": dataset_id},
                    )
                self._check_class(ontology, region.class_id, dataset_id)
                mask = self._check_mask(decode_coco(region.coverage_rle), dataset)
                if not mask.any():
                    raise AppError(
                        "VALIDATION_ERROR",
                        f"snapshot region {region.region_id} is empty",
                        status_code=422,
                        target={"dataset_id": dataset_id, "region_id": region.region_id},
                    )
                copy = region.model_copy(deep=True)
                copy.dataset_id = dataset_id
                copy.coverage_rle = encode_coco(mask)
                copy.bbox_xywh = bounding_box(mask)
                copy.area = int(mask.sum())
                restored.append(copy)
                try:
                    highest_seq = max(highest_seq, int(region.region_id.split("_", 1)[1]) + 1)
                except ValueError:
                    pass
            region_set.regions = restored
            region_set.overlap_reviews = {
                key: OverlapReview.model_validate(value) for key, value in overlap_reviews.items()
            }
            region_set.next_region_seq = max(
                highest_seq, next_region_seq or 0, region_set.next_region_seq
            )
            return {"reason": reason, "region_ids": ids, "reviews": len(overlap_reviews)}

        return self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="regions.restore",
            actor=actor,
            region_ids=[r.region_id for r in regions],
        )

    def duplicate_region(
        self,
        project_id: str,
        dataset_id: str,
        region_id: str,
        expected_revision: int | None,
        *,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, Region]:
        created: dict[str, Region] = {}

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            region = self._require(region_set, region_id)
            for other in region_set.regions:
                if other.z_order > region.z_order:
                    other.z_order += 1
            new = self._new_region(
                region_set,
                dataset,
                region.class_id,
                region_mask(region),
                source="manual",
                provenance={"duplicated_from": region_id},
                name=f"{region.name} copy" if region.name else None,
                z_order=region.z_order + 1,
            )
            created["region"] = new
            return {"new_region_id": new.region_id}

        dataset, region_set = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="region.duplicate",
            actor=actor,
            region_ids=[region_id],
        )
        return dataset, region_set, created["region"]
