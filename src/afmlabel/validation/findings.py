# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Unified Findings model.

A *Finding* is a validation/review item, never geometry. The list is
computed live from the canonical region set every time it is requested,
so it can never be stale; what *can* be stale is the stored validation
report that gates export (``dataset.validated_revision``), and that state
is reported separately so the UI never shows a green report for a
revision that no longer matches the annotation.

Three kinds of operations stay separate on purpose:

* geometry operations (brush, Merge, Split, Subtract by Order, z-order)
  change regions and therefore change or remove Findings on recomputation;
* the validation operation stores a report for the current revision;
* the review operation only changes the review state of an overlap
  Finding. It never touches geometry.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import ClassOntology
from afmlabel.domain.region import RegionSet
from afmlabel.domain.validation import OverlapGroup, ValidationIssue
from afmlabel.overlaps.engine import missing_components, overlap_groups
from afmlabel.settings import BoundaryMode
from afmlabel.validation.engine import validate

FindingType = Literal[
    "overlap",
    "missing_coverage",
    "hidden_region",
    "invalid_class",
    "boundary_policy",
    "source_pair",
    "lossy_import",
    "model_corrected",
    "source_replaced",
    "composition",
]

Severity = Literal["blocking", "warning"]
ReviewState = Literal["unreviewed", "reviewed", "not_applicable"]

TYPE_LABELS: dict[str, str] = {
    "overlap": "Overlap",
    "missing_coverage": "Missing coverage",
    "hidden_region": "Hidden region",
    "invalid_class": "Invalid class",
    "boundary_policy": "Boundary policy",
    "source_pair": "Source pair",
    "lossy_import": "Lossy import",
    "model_corrected": "Model proposal corrected",
    "source_replaced": "Source replaced",
    "composition": "Composition",
}

_CODE_TO_TYPE: dict[str, FindingType] = {
    "OVERLAP_UNREVIEWED": "overlap",
    "OVERLAP_MULTIPLE": "overlap",
    "UNLABELED_PIXELS": "missing_coverage",
    "REGION_HIDDEN": "hidden_region",
    "CLASS_REFERENCE_INVALID": "invalid_class",
    "REGION_REFERENCE_INVALID": "invalid_class",
    "EXPORT_PROFILE_INVALID": "invalid_class",
    "BOUNDARY_POLICY_VIOLATION": "boundary_policy",
    "PAIR_MISSING_IMAGE": "source_pair",
    "PAIR_DIMENSION_MISMATCH": "source_pair",
    "TSWAT_IMPORT_LOSSY": "lossy_import",
    "MODEL_UNAVAILABLE": "model_corrected",
    "SOURCE_REPLACEMENT_INCOMPATIBLE": "source_replaced",
    "VALIDATION_FAILED": "composition",
}


class Finding(BaseModel):
    finding_id: str
    type: FindingType
    type_label: str
    severity: Severity
    review_state: ReviewState
    #: Only overlap findings can be acknowledged; the rest need a correction.
    reviewable: bool
    #: True when review cannot waive the condition (it stays blocking).
    requires_correction: bool
    code: str
    title: str
    message: str
    region_ids: list[str] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    bbox_xywh: list[int] | None = None
    group_id: str | None = None
    pixel_count: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    #: Deterministic position in the default ordering (0-based).
    rank: int = 0


class FindingTypeCount(BaseModel):
    type: FindingType
    label: str
    count: int


class FindingsCounts(BaseModel):
    blocking_unreviewed: int = 0
    warning_unreviewed: int = 0
    reviewed: int = 0
    total: int = 0


class FindingsResponse(BaseModel):
    dataset_id: str
    dataset_revision: int
    #: Revision of the stored validation report, ``None`` when it was never
    #: run or the stored report is not ``valid``.
    validated_revision: int | None
    #: A stored report exists but was computed for an older revision.
    validation_stale: bool
    #: Status of the stored report (``valid``/``blocked``) or ``None``.
    stored_validation_status: str | None
    stored_validation_revision: int | None
    require_overlap_review: bool
    boundary_mode: str
    coverage_percent: float
    unlabeled_pixels: int
    findings: list[Finding] = Field(default_factory=list)
    types: list[FindingTypeCount] = Field(default_factory=list)
    counts: FindingsCounts = Field(default_factory=FindingsCounts)


def _group_rank(finding: Finding) -> int:
    if finding.review_state == "reviewed":
        return 2
    return 0 if finding.severity == "blocking" else 1


def _sort_key(finding: Finding) -> tuple[Any, ...]:
    bbox = finding.bbox_xywh or [10**9, 10**9, 0, 0]
    return (_group_rank(finding), finding.type, bbox[1], bbox[0], finding.finding_id)


def _issue_finding(issue: ValidationIssue, severity: Severity, index: int) -> Finding | None:
    kind = _CODE_TO_TYPE.get(issue.code)
    if kind is None or kind == "overlap" or issue.code == "UNLABELED_PIXELS":
        # Overlaps and missing pixels are emitted per group/component below.
        return None
    return Finding(
        finding_id=f"{kind}:{issue.code}:{index}",
        type=kind,
        type_label=TYPE_LABELS[kind],
        severity=severity,
        review_state="not_applicable",
        reviewable=False,
        requires_correction=severity == "blocking",
        code=issue.code,
        title=TYPE_LABELS[kind],
        message=issue.message,
        region_ids=list(issue.region_ids),
        class_ids=list(issue.class_ids),
        details=dict(issue.details),
    )


def _overlap_finding(group: OverlapGroup, require_review: bool) -> Finding:
    reviewed = group.review_state != "unreviewed"
    severity: Severity = "blocking" if (require_review and not reviewed) else "warning"
    state = {
        "unreviewed": "Unreviewed",
        "reviewed_resolved": "Reviewed: resolved by ordering",
        "reviewed_intentional": "Reviewed: intentional",
        "reviewed_fixed_by_edit": "Reviewed: fixed by edit",
    }[group.review_state]
    return Finding(
        finding_id=f"overlap:{group.group_id}",
        type="overlap",
        type_label=TYPE_LABELS["overlap"],
        severity=severity,
        review_state="reviewed" if reviewed else "unreviewed",
        reviewable=True,
        requires_correction=False,
        code="OVERLAP_UNREVIEWED" if not reviewed else "OVERLAP_MULTIPLE",
        title=f"Overlap ×{group.max_multiplicity} · {group.pixel_count} px",
        message=(
            f"{' / '.join(group.region_ids)} overlap; z-order winner: "
            f"{group.winning_region_id or '–'} · {state}"
        ),
        region_ids=list(group.region_ids),
        class_ids=list(group.class_ids),
        bbox_xywh=list(group.bbox_xywh),
        group_id=group.group_id,
        pixel_count=group.pixel_count,
        details={
            "max_multiplicity": group.max_multiplicity,
            "winning_region_id": group.winning_region_id,
            "overlap_review_state": group.review_state,
            "reviewed_at": group.reviewed_at,
            "note": group.note,
        },
    )


def build_findings(
    dataset: Dataset,
    region_set: RegionSet,
    ontology: ClassOntology,
    *,
    project_revision: int,
    boundary_mode: BoundaryMode,
    require_overlap_review: bool,
    stored_status: str | None,
    stored_revision: int | None,
    missing_limit: int = 50,
) -> FindingsResponse:
    report, composition = validate(
        dataset,
        region_set,
        ontology,
        project_revision=project_revision,
        boundary_mode=boundary_mode,
        require_overlap_review=require_overlap_review,
    )
    findings: list[Finding] = []
    for index, issue in enumerate(report.blocking):
        finding = _issue_finding(issue, "blocking", index)
        if finding is not None:
            findings.append(finding)
    for index, issue in enumerate(report.warnings):
        finding = _issue_finding(issue, "warning", index)
        if finding is not None:
            findings.append(finding)
    for group in overlap_groups(composition, region_set):
        findings.append(_overlap_finding(group, require_overlap_review))
    components, component_count = missing_components(composition, limit=missing_limit)
    for index, component in enumerate(components):
        findings.append(
            Finding(
                finding_id=f"missing:{component.bbox_xywh[0]}:{component.bbox_xywh[1]}:{index}",
                type="missing_coverage",
                type_label=TYPE_LABELS["missing_coverage"],
                severity="blocking",
                review_state="not_applicable",
                reviewable=False,
                requires_correction=True,
                code="UNLABELED_PIXELS",
                title=f"Missing coverage · {component.pixel_count} px",
                message=(
                    f"{component.pixel_count} unlabelled pixel(s) at x {component.bbox_xywh[0]}, "
                    f"y {component.bbox_xywh[1]} "
                    f"({component.bbox_xywh[2]}×{component.bbox_xywh[3]})"
                    + (
                        "; approve a Boundary fill or annotate them"
                        if boundary_mode == "fill_unlabeled_after_approval"
                        else ""
                    )
                ),
                bbox_xywh=list(component.bbox_xywh),
                pixel_count=component.pixel_count,
                details={"component_count": component_count},
            )
        )
    findings.sort(key=_sort_key)
    for rank, finding in enumerate(findings):
        finding.rank = rank
    counts = FindingsCounts(total=len(findings))
    type_counts: dict[str, int] = {}
    for finding in findings:
        type_counts[finding.type] = type_counts.get(finding.type, 0) + 1
        if finding.review_state == "reviewed":
            counts.reviewed += 1
        elif finding.severity == "blocking":
            counts.blocking_unreviewed += 1
        else:
            counts.warning_unreviewed += 1
    return FindingsResponse(
        dataset_id=dataset.dataset_id,
        dataset_revision=dataset.revision,
        validated_revision=dataset.validated_revision,
        validation_stale=stored_revision is not None and stored_revision != dataset.revision,
        stored_validation_status=stored_status,
        stored_validation_revision=stored_revision,
        require_overlap_review=require_overlap_review,
        boundary_mode=boundary_mode,
        coverage_percent=report.coverage.coverage_percent,
        unlabeled_pixels=report.coverage.unlabeled_pixels,
        findings=findings,
        types=[
            FindingTypeCount(type=kind, label=TYPE_LABELS[kind], count=count)  # type: ignore[arg-type]
            for kind, count in sorted(type_counts.items())
        ],
        counts=counts,
    )
