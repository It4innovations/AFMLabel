# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Validation, overlap review and Boundary fill as revisioned operations."""

from __future__ import annotations

from typing import Any

from afmlabel.annotations.service import RegionService
from afmlabel.domain.common import utc_now_iso
from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import ClassOntology
from afmlabel.domain.region import OverlapReview, OverlapReviewState, Region, RegionSet
from afmlabel.domain.validation import OverlapReport, ValidationReport
from afmlabel.errors import AppError, NotFoundError
from afmlabel.overlaps.engine import (
    build_overlap_report,
    compose,
    overlap_groups,
    prune_stale_reviews,
)
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_json, save_model
from afmlabel.persistence.migrations import migrate
from afmlabel.projects.datasets import DatasetStore
from afmlabel.projects.store import ProjectStore
from afmlabel.settings import BoundaryMode
from afmlabel.validation.engine import boundary_fill_mask, validate
from afmlabel.validation.findings import FindingsResponse, build_findings


class ValidationService:
    def __init__(
        self, projects: ProjectStore, datasets: DatasetStore, regions: RegionService
    ) -> None:
        self._projects = projects
        self._datasets = datasets
        self._regions = regions

    # ---------------------------------------------------------------- policy

    def policy(
        self, project_id: str, settings_boundary: BoundaryMode, settings_review: bool
    ) -> tuple[BoundaryMode, bool]:
        project = self._projects.get_project(project_id)
        boundary = project.config.boundary_mode or settings_boundary
        review = (
            project.config.require_overlap_review
            if project.config.require_overlap_review is not None
            else settings_review
        )
        return boundary, review

    # -------------------------------------------------------------- overlaps

    def overlap_report(self, project_id: str, dataset_id: str) -> OverlapReport:
        dataset, region_set = self._regions.load(project_id, dataset_id)
        ontology = self._projects.get_ontology(project_id)
        return build_overlap_report(
            dataset_id, dataset.revision, region_set, ontology, dataset.height, dataset.width
        )

    def review_overlap(
        self,
        project_id: str,
        dataset_id: str,
        group_id: str,
        expected_revision: int | None,
        *,
        state: OverlapReviewState,
        note: str | None,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, OverlapReport]:
        ontology = self._projects.get_ontology(project_id)

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            composition = compose(region_set, ontology, dataset.height, dataset.width)
            groups = overlap_groups(composition, region_set)
            group = next((g for g in groups if g.group_id == group_id), None)
            if group is None:
                raise NotFoundError(
                    "OVERLAP_MULTIPLE",
                    f"overlap group {group_id} no longer exists (the overlap changed or was fixed)",
                    target={"dataset_id": dataset_id, "group_id": group_id},
                    recovery="refresh the findings list",
                )
            removed = prune_stale_reviews(region_set, groups)
            if state == "unreviewed":
                region_set.overlap_reviews.pop(group_id, None)
            else:
                region_set.overlap_reviews[group_id] = OverlapReview(
                    state=state,
                    reviewed_at=utc_now_iso(),
                    note=note,
                    region_ids=list(group.region_ids),
                )
            return {
                "group_id": group_id,
                "state": state,
                "region_ids": group.region_ids,
                "pixel_count": group.pixel_count,
                "max_multiplicity": group.max_multiplicity,
                "pruned_reviews": removed,
            }

        dataset, region_set = self._regions.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="overlap.review",
            actor=actor,
        )
        report = build_overlap_report(
            dataset_id, dataset.revision, region_set, ontology, dataset.height, dataset.width
        )
        return dataset, region_set, report

    def review_overlaps_bulk(
        self,
        project_id: str,
        dataset_id: str,
        group_ids: list[str],
        expected_revision: int | None,
        *,
        state: OverlapReviewState,
        note: str | None,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, list[str], list[str]]:
        """Change the review state of several overlap groups in one revisioned
        mutation. Review never touches geometry;
        groups that no longer exist are reported, not silently ignored."""
        ontology = self._projects.get_ontology(project_id)
        applied: list[str] = []
        missing: list[str] = []

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            composition = compose(region_set, ontology, dataset.height, dataset.width)
            groups = {g.group_id: g for g in overlap_groups(composition, region_set)}
            prune_stale_reviews(region_set, list(groups.values()))
            for group_id in dict.fromkeys(group_ids):
                group = groups.get(group_id)
                if group is None:
                    missing.append(group_id)
                    continue
                if state == "unreviewed":
                    region_set.overlap_reviews.pop(group_id, None)
                else:
                    region_set.overlap_reviews[group_id] = OverlapReview(
                        state=state,
                        reviewed_at=utc_now_iso(),
                        note=note,
                        region_ids=list(group.region_ids),
                    )
                applied.append(group_id)
            if not applied:
                raise NotFoundError(
                    "OVERLAP_MULTIPLE",
                    "none of the selected overlap groups exist any more "
                    "(the overlaps changed or were fixed)",
                    target={"dataset_id": dataset_id},
                    recovery="refresh the findings list",
                    details={"missing_group_ids": missing},
                )
            return {"state": state, "group_ids": applied, "missing_group_ids": missing}

        dataset, region_set = self._regions.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="overlap.review_bulk",
            actor=actor,
        )
        return dataset, region_set, applied, missing

    # -------------------------------------------------------------- findings

    def findings(
        self,
        project_id: str,
        dataset_id: str,
        *,
        boundary_mode: BoundaryMode,
        require_overlap_review: bool,
    ) -> FindingsResponse:
        project = self._projects.get_project(project_id)
        dataset, region_set = self._regions.load(project_id, dataset_id)
        ontology = self._projects.get_ontology(project_id)
        stored = self.latest_report(project_id, dataset_id)
        return build_findings(
            dataset,
            region_set,
            ontology,
            project_revision=project.revision,
            boundary_mode=boundary_mode,
            require_overlap_review=require_overlap_review,
            stored_status=stored.status if stored else None,
            stored_revision=stored.dataset_revision if stored else None,
        )

    # ------------------------------------------------------------ validation

    def latest_report(self, project_id: str, dataset_id: str) -> ValidationReport | None:
        ddir = self._datasets.dataset_dir(project_id, dataset_id)
        path = paths.validation_file(ddir)
        if not path.exists():
            return None
        return ValidationReport.model_validate(migrate("validation", load_json(path)))

    def run_validation(
        self,
        project_id: str,
        dataset_id: str,
        *,
        boundary_mode: BoundaryMode,
        require_overlap_review: bool,
        actor: str | None,
    ) -> ValidationReport:
        project = self._projects.get_project(project_id)
        dataset, region_set = self._regions.load(project_id, dataset_id)
        ontology = self._projects.get_ontology(project_id)
        report, _ = validate(
            dataset,
            region_set,
            ontology,
            project_revision=project.revision,
            boundary_mode=boundary_mode,
            require_overlap_review=require_overlap_review,
        )
        ddir = self._datasets.dataset_dir(project_id, dataset_id)
        save_model(paths.validation_file(ddir), report)
        self._datasets.set_validated_revision(
            project_id, dataset_id, report.dataset_revision if report.status == "valid" else None
        )
        self._projects.record_event(
            project_id,
            operation="dataset.validate",
            dataset_id=dataset_id,
            new_revision=dataset.revision,
            source="system",
            actor=actor,
            details={
                "status": report.status,
                "blocking": [i.code for i in report.blocking],
                "warnings": [i.code for i in report.warnings],
                "coverage_percent": report.coverage.coverage_percent,
                "composition_sha256": report.composition_sha256,
            },
        )
        return report

    # --------------------------------------------------------- boundary fill

    def boundary_fill_preview(
        self, project_id: str, dataset_id: str, *, boundary_mode: BoundaryMode
    ) -> dict[str, Any]:
        dataset, region_set = self._regions.load(project_id, dataset_id)
        ontology = self._projects.get_ontology(project_id)
        boundary = ontology.boundary_class()
        composition = compose(region_set, ontology, dataset.height, dataset.width)
        mask = boundary_fill_mask(composition)
        from afmlabel.persistence.rle import bounding_box

        return {
            "dataset_revision": dataset.revision,
            "allowed": boundary_mode == "fill_unlabeled_after_approval" and boundary is not None,
            "boundary_mode": boundary_mode,
            "boundary_class_id": boundary.class_id if boundary else None,
            "boundary_class_name": boundary.name if boundary else None,
            "pixel_count": int(mask.sum()),
            "bbox_xywh": bounding_box(mask),
            "reason": None
            if boundary_mode == "fill_unlabeled_after_approval" and boundary is not None
            else (
                "boundary_mode is not 'fill_unlabeled_after_approval'"
                if boundary_mode != "fill_unlabeled_after_approval"
                else "the project has no enabled Boundary class"
            ),
        }

    def boundary_fill(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        *,
        confirmed_pixel_count: int,
        boundary_mode: BoundaryMode,
        actor: str | None,
    ) -> tuple[Dataset, RegionSet, Region]:
        ontology: ClassOntology = self._projects.get_ontology(project_id)
        boundary = ontology.boundary_class()
        if boundary_mode != "fill_unlabeled_after_approval":
            raise AppError(
                "BOUNDARY_POLICY_VIOLATION",
                f"boundary fill is not allowed under boundary_mode {boundary_mode!r}",
                status_code=409,
                target={"project_id": project_id, "dataset_id": dataset_id},
                recovery="set annotation.boundary_mode to fill_unlabeled_after_approval",
            )
        if boundary is None:
            raise AppError(
                "BOUNDARY_POLICY_VIOLATION",
                "the project has no enabled Boundary class",
                status_code=409,
                target={"project_id": project_id},
                recovery="create a class with semantic role Boundary",
            )
        created: dict[str, Region] = {}

        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            composition = compose(region_set, ontology, dataset.height, dataset.width)
            mask = boundary_fill_mask(composition)
            count = int(mask.sum())
            if count == 0:
                raise AppError(
                    "UNLABELED_PIXELS",
                    "there are no unlabeled pixels to fill",
                    status_code=409,
                    target={"dataset_id": dataset_id},
                )
            if count != confirmed_pixel_count:
                raise AppError(
                    "UNLABELED_PIXELS",
                    f"the unlabeled pixel count is {count}, not {confirmed_pixel_count}; "
                    "the annotation changed since the preview",
                    status_code=409,
                    target={"dataset_id": dataset_id},
                    recovery="request a new preview and confirm the current count",
                    details={"pixel_count": count},
                )
            # Boundary fill sits at the very bottom: it can never win over a
            # material label, and it only covers pixels nobody labelled.
            for region in region_set.regions:
                region.z_order += 1
            region = RegionService._new_region(
                region_set,
                dataset,
                boundary.class_id,
                mask,
                source="boundary_fill",
                provenance={
                    "policy": "fill_unlabeled_after_approval",
                    "approved_pixel_count": count,
                    "approved_at": utc_now_iso(),
                },
                name="Boundary fill",
                z_order=1,
            )
            created["region"] = region
            return {"region_id": region.region_id, "pixel_count": count}

        dataset, region_set = self._regions.mutate(
            project_id,
            dataset_id,
            expected_revision,
            fn,
            operation="boundary.fill",
            actor=actor,
            source="boundary_fill",
            class_ids=[boundary.class_id],
        )
        return dataset, region_set, created["region"]
