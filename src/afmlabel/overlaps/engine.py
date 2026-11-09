# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Overlap raster, connected overlap groups and composition.

Definitions:

    R(p) = set of *composable* regions covering pixel p
    composable = region visible and its class enabled

A pixel with ``|R(p)| > 1`` is overlapped. Overlapped pixels are grouped
into 4-connected components; each group records its participating regions,
classes, pixel count, bounding box and maximum multiplicity. The group id
is a digest of the participant ids and the group's pixel set, so it is
stable under z-order changes (which do not alter *whether* pixels overlap)
and changes as soon as an edit changes the overlapping pixels -- a review
never silently survives a coverage edit.

Composition: the composable region with the highest
z-order wins; unlabeled pixels stay ``0`` and are never mapped to a class.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import numpy as np

from afmlabel.domain.project import ClassOntology
from afmlabel.domain.region import Region, RegionSet
from afmlabel.domain.validation import (
    CoverageSummary,
    MissingComponent,
    OverlapGroup,
    OverlapReport,
)
from afmlabel.persistence.rle import bounding_box, decode_coco, label_components

UNLABELED = 0


@dataclass
class DecodedRegion:
    region: Region
    mask: np.ndarray


@dataclass
class Composition:
    label_map: np.ndarray  # int32 (H, W), 0 = unlabeled
    winner_map: np.ndarray  # int32 index into ``regions`` (+1), 0 = none
    multiplicity: np.ndarray  # uint16 (H, W)
    regions: list[DecodedRegion] = field(default_factory=list)

    @property
    def total_pixels(self) -> int:
        return int(self.label_map.size)

    @property
    def unlabeled_mask(self) -> np.ndarray:
        result: np.ndarray = self.label_map == UNLABELED
        return result

    def coverage(self) -> CoverageSummary:
        total = self.total_pixels
        labeled = int((self.label_map != UNLABELED).sum())
        overlapped = int((self.multiplicity > 1).sum())
        return CoverageSummary(
            total_pixels=total,
            labeled_pixels=labeled,
            unlabeled_pixels=total - labeled,
            coverage_percent=round(100.0 * labeled / total, 4) if total else 0.0,
            overlapped_pixels=overlapped,
            max_multiplicity=int(self.multiplicity.max()) if self.multiplicity.size else 0,
        )

    def class_histogram(self) -> dict[int, int]:
        values, counts = np.unique(self.label_map, return_counts=True)
        return {int(v): int(c) for v, c in zip(values, counts, strict=True) if int(v) != UNLABELED}

    def sha256(self) -> str:
        return hashlib.sha256(self.label_map.astype("<i4").tobytes()).hexdigest()


def composable_regions(region_set: RegionSet, ontology: ClassOntology) -> list[Region]:
    enabled = {c.class_id for c in ontology.classes if c.enabled}
    return [r for r in region_set.sorted_by_z() if r.visible and r.class_id in enabled]


def compose(region_set: RegionSet, ontology: ClassOntology, height: int, width: int) -> Composition:
    label_map = np.zeros((height, width), dtype=np.int32)
    winner_map = np.zeros((height, width), dtype=np.int32)
    multiplicity = np.zeros((height, width), dtype=np.uint16)
    decoded: list[DecodedRegion] = []
    for index, region in enumerate(composable_regions(region_set, ontology), start=1):
        mask = decode_coco(region.coverage_rle)
        if mask.shape != (height, width):
            raise ValueError(
                f"region {region.region_id} mask {mask.shape} does not match {(height, width)}"
            )
        decoded.append(DecodedRegion(region=region, mask=mask))
        # Ascending z-order: later (higher) regions overwrite earlier ones.
        label_map[mask] = region.class_id
        winner_map[mask] = index
        multiplicity[mask] += 1
    return Composition(
        label_map=label_map, winner_map=winner_map, multiplicity=multiplicity, regions=decoded
    )


def _group_digest(region_ids: list[str], pixels: np.ndarray) -> str:
    digest = hashlib.sha256()
    digest.update(",".join(sorted(region_ids)).encode("utf-8"))
    digest.update(np.packbits(pixels.ravel()).tobytes())
    return f"ovl_{digest.hexdigest()[:12]}"


def overlap_groups(composition: Composition, region_set: RegionSet) -> list[OverlapGroup]:
    overlapped = composition.multiplicity > 1
    if not overlapped.any():
        return []
    labels, count = label_components(overlapped)
    groups: list[OverlapGroup] = []
    for index in range(1, count + 1):
        pixels = labels == index
        participants = [d.region for d in composition.regions if bool((d.mask & pixels).any())]
        region_ids = [r.region_id for r in participants]
        winner = max(participants, key=lambda r: r.z_order) if participants else None
        group_id = _group_digest(region_ids, pixels)
        review = region_set.overlap_reviews.get(group_id)
        groups.append(
            OverlapGroup(
                group_id=group_id,
                pixel_count=int(pixels.sum()),
                max_multiplicity=int(composition.multiplicity[pixels].max()),
                bbox_xywh=bounding_box(pixels),
                region_ids=region_ids,
                class_ids=sorted({r.class_id for r in participants}),
                review_state=review.state if review else "unreviewed",
                reviewed_at=review.reviewed_at if review else None,
                note=review.note if review else None,
                winning_region_id=winner.region_id if winner else None,
            )
        )
    groups.sort(key=lambda g: (-g.pixel_count, g.group_id))
    return groups


def missing_components(
    composition: Composition, limit: int = 50
) -> tuple[list[MissingComponent], int]:
    unlabeled = composition.unlabeled_mask
    if not unlabeled.any():
        return [], 0
    labels, count = label_components(unlabeled)
    components = [
        MissingComponent(pixel_count=int((labels == i).sum()), bbox_xywh=bounding_box(labels == i))
        for i in range(1, count + 1)
    ]
    components.sort(key=lambda c: -c.pixel_count)
    return components[:limit], count


def build_overlap_report(
    dataset_id: str,
    dataset_revision: int,
    region_set: RegionSet,
    ontology: ClassOntology,
    height: int,
    width: int,
) -> OverlapReport:
    composition = compose(region_set, ontology, height, width)
    groups = overlap_groups(composition, region_set)
    missing, missing_count = missing_components(composition)
    return OverlapReport(
        dataset_id=dataset_id,
        dataset_revision=dataset_revision,
        coverage=composition.coverage(),
        groups=groups,
        unreviewed_count=sum(1 for g in groups if g.review_state == "unreviewed"),
        missing_components=missing,
        missing_component_count=missing_count,
        hidden_region_ids=[r.region_id for r in region_set.regions if not r.visible],
    )


def prune_stale_reviews(region_set: RegionSet, groups: list[OverlapGroup]) -> list[str]:
    """Drop reviews whose group no longer exists (the overlap was fixed by an
    edit); returns the removed group ids for the audit log."""
    live = {g.group_id for g in groups}
    stale = [gid for gid in region_set.overlap_reviews if gid not in live]
    for gid in stale:
        del region_set.overlap_reviews[gid]
    return stale
