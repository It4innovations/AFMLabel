# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Dataset validation and Boundary policy.

``validate`` never mutates anything; it composes the regions, evaluates
every blocking condition and warning, and returns a report whose
``status`` is ``valid`` only when nothing blocks. The final mask is the
composition's label map (exactly one class per pixel, never ``0`` for a
valid dataset).
"""

from __future__ import annotations

import numpy as np

from afmlabel.domain.common import utc_now_iso
from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import ClassOntology, SemanticRole
from afmlabel.domain.region import RegionSet
from afmlabel.domain.validation import ValidationIssue, ValidationReport
from afmlabel.overlaps.engine import (
    Composition,
    compose,
    missing_components,
    overlap_groups,
)
from afmlabel.settings import BoundaryMode


def validate(
    dataset: Dataset,
    region_set: RegionSet,
    ontology: ClassOntology,
    *,
    project_revision: int,
    boundary_mode: BoundaryMode,
    require_overlap_review: bool,
) -> tuple[ValidationReport, Composition]:
    blocking: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []

    if dataset.status != "ready":
        blocking.append(
            ValidationIssue(
                code="PAIR_MISSING_IMAGE",
                message=f"source pair is blocked: {dataset.block_reason or 'unknown reason'}",
            )
        )
    sizes = {(s.width, s.height) for s in dataset.channels}
    if len(sizes) > 1:
        blocking.append(
            ValidationIssue(
                code="PAIR_DIMENSION_MISMATCH",
                message="source channel dimensions differ ("
                + ", ".join(f"{s.channel} {s.width}x{s.height}" for s in dataset.channels)
                + ")",
            )
        )

    known = {c.class_id: c for c in ontology.classes}
    invalid = [r for r in region_set.regions if r.class_id not in known]
    if invalid:
        blocking.append(
            ValidationIssue(
                code="CLASS_REFERENCE_INVALID",
                message=f"{len(invalid)} region(s) reference a class that no longer exists",
                region_ids=[r.region_id for r in invalid],
                class_ids=sorted({r.class_id for r in invalid}),
            )
        )
    disabled = [
        r for r in region_set.regions if r.class_id in known and not known[r.class_id].enabled
    ]
    if disabled:
        blocking.append(
            ValidationIssue(
                code="CLASS_REFERENCE_INVALID",
                message=f"{len(disabled)} region(s) use a disabled class; enable the class or "
                "reassign/delete the regions",
                region_ids=[r.region_id for r in disabled],
                class_ids=sorted({r.class_id for r in disabled}),
            )
        )
    hidden = [r for r in region_set.regions if not r.visible]
    if hidden:
        blocking.append(
            ValidationIssue(
                code="REGION_HIDDEN",
                message=f"{len(hidden)} region(s) are hidden; hidden regions are excluded from "
                "composition, so show or delete them before export",
                region_ids=[r.region_id for r in hidden],
            )
        )
    try:
        ontology.validate_unique()
    except ValueError as exc:
        blocking.append(ValidationIssue(code="EXPORT_PROFILE_INVALID", message=str(exc)))

    ids = [r.region_id for r in region_set.regions]
    if len(set(ids)) != len(ids):
        blocking.append(
            ValidationIssue(code="REGION_REFERENCE_INVALID", message="duplicate region ids")
        )

    composition = compose(region_set, ontology, dataset.height, dataset.width)
    groups = overlap_groups(composition, region_set)
    coverage = composition.coverage()

    unreviewed = [g for g in groups if g.review_state == "unreviewed"]
    if unreviewed:
        issue = ValidationIssue(
            code="OVERLAP_UNREVIEWED",
            message=f"{len(unreviewed)} overlap group(s) have not been reviewed",
            group_ids=[g.group_id for g in unreviewed],
            region_ids=sorted({rid for g in unreviewed for rid in g.region_ids}),
            details={"max_multiplicity": max(g.max_multiplicity for g in unreviewed)},
        )
        (blocking if require_overlap_review else warnings).append(issue)
    intentional = [g for g in groups if g.review_state == "reviewed_intentional"]
    if intentional:
        warnings.append(
            ValidationIssue(
                code="OVERLAP_MULTIPLE",
                message=f"{len(intentional)} overlap group(s) were accepted as intentional; "
                "z-order decides the final class there",
                group_ids=[g.group_id for g in intentional],
            )
        )

    boundary = ontology.boundary_class()
    boundary_regions = [
        r
        for r in region_set.regions
        if r.class_id in known and known[r.class_id].semantic_role == SemanticRole.BOUNDARY
    ]
    if boundary_mode == "disabled" and boundary_regions:
        blocking.append(
            ValidationIssue(
                code="BOUNDARY_POLICY_VIOLATION",
                message="boundary_mode is 'disabled' but Boundary-class regions exist",
                region_ids=[r.region_id for r in boundary_regions],
            )
        )
    if (
        boundary_mode == "fill_unlabeled_after_approval"
        and boundary is None
        and coverage.unlabeled_pixels
    ):
        blocking.append(
            ValidationIssue(
                code="BOUNDARY_POLICY_VIOLATION",
                message="boundary_mode is 'fill_unlabeled_after_approval' but the project has no "
                "enabled Boundary class to fill with",
            )
        )
    if coverage.unlabeled_pixels:
        _, component_count = missing_components(composition, limit=1)
        message = f"{coverage.unlabeled_pixels} pixel(s) are unlabeled in {component_count} area(s)"
        if boundary_mode == "fill_unlabeled_after_approval" and boundary is not None:
            message += "; approve a Boundary fill or annotate them"
        blocking.append(
            ValidationIssue(
                code="UNLABELED_PIXELS",
                message=message,
                details={
                    "unlabeled_pixels": coverage.unlabeled_pixels,
                    "components": component_count,
                },
            )
        )
    fills = [r for r in region_set.regions if r.source == "boundary_fill"]
    if fills:
        warnings.append(
            ValidationIssue(
                code="BOUNDARY_POLICY_VIOLATION",
                message=f"{len(fills)} region(s) were created by approved Boundary fill",
                region_ids=[r.region_id for r in fills],
            )
        )
    for source_image in dataset.channels:
        if source_image.lossy_format:
            warnings.append(
                ValidationIssue(
                    code="TSWAT_IMPORT_LOSSY",
                    message=f"{source_image.channel} source was imported from a lossy format",
                )
            )
    lossy = [r for r in region_set.regions if r.source == "lossy_import"]
    if lossy:
        warnings.append(
            ValidationIssue(
                code="TSWAT_IMPORT_LOSSY",
                message=f"{len(lossy)} region(s) come from a flattened raster import; "
                "original overlaps could not be reconstructed",
                region_ids=[r.region_id for r in lossy],
            )
        )
    corrected = [r for r in region_set.regions if r.source == "sam_proposal" and r.user_corrected]
    if corrected:
        warnings.append(
            ValidationIssue(
                code="MODEL_UNAVAILABLE",
                message=f"{len(corrected)} model proposal(s) were manually corrected",
                region_ids=[r.region_id for r in corrected],
            )
        )
    if dataset.replacements and region_set.regions:
        warnings.append(
            ValidationIssue(
                code="SOURCE_REPLACEMENT_INCOMPATIBLE",
                message=(
                    f"a source image was replaced {len(dataset.replacements)} time(s) after import"
                ),
            )
        )

    # Exactly-one-label invariant on the composed map.
    if not blocking:
        label_map = composition.label_map
        if (label_map == 0).any() or not np.isin(label_map, list(known)).all():
            blocking.append(
                ValidationIssue(
                    code="VALIDATION_FAILED",
                    message="composed mask violates the exactly-one-valid-class invariant",
                )
            )

    report = ValidationReport(
        dataset_id=dataset.dataset_id,
        dataset_revision=dataset.revision,
        project_revision=project_revision,
        computed_at=utc_now_iso(),
        status="blocked" if blocking else "valid",
        blocking=blocking,
        warnings=warnings,
        coverage=coverage,
        overlap_group_count=len(groups),
        unreviewed_overlap_count=len(unreviewed),
        class_pixel_histogram=composition.class_histogram(),
        region_count=len(region_set.regions),
        boundary_mode=boundary_mode,
        require_overlap_review=require_overlap_review,
        composition_sha256=composition.sha256() if not blocking else None,
    )
    return report, composition


def boundary_fill_mask(composition: Composition) -> np.ndarray:
    """Pixels a Boundary fill would receive: exactly the unlabeled ones.

    By construction the fill never touches a pixel that already has a
    resolved material label.
    """
    return composition.unlabeled_mask.copy()
