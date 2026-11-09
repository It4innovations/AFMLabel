# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Lossy import of a flattened label raster.

A flattened mask lost every overlap and region identity. The importer
still helps when nothing else exists: every distinct non-zero value maps
to a class chosen by the user, each connected component becomes its own
region, and everything is marked ``source="lossy_import"`` so validation
and export keep warning about it.

Rules added by the upgrade:

* nothing is discarded silently -- every raster value must be mapped to a
  class or explicitly marked *do not import*, and discarding needs an
  explicit confirmation flag;
* the proposed class for a value is the project's ``buffer`` class (or the
  configured default import class); when neither exists the UI must ask;
* a dimension mismatch blocks the import, it is never resampled;
* the commit records file name, checksum, dimensions, source values, the
  mapping used and the importer version.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from afmlabel.annotations.service import RegionService
from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import ClassOntology, ProjectConfig, find_default_import_class
from afmlabel.domain.region import RegionSet
from afmlabel.errors import AppError
from afmlabel.images.decode import ImageDecodeError, decode_image, sha256_of_file
from afmlabel.persistence.rle import bounding_box, split_components

IMPORTER_VERSION = "lossy-raster/afmlabel-2"

LOSSY_WARNING = (
    "Flattened raster import is lossy: overlaps and hidden regions in the original "
    "annotation cannot be reconstructed. Regions will be marked as lossy_import."
)


class LossyValue(BaseModel):
    value: int
    pixel_count: int
    component_count: int
    #: Class proposed by the server (Buffer / configured default), or None.
    suggested_class_id: int | None = None


class LossyPreview(BaseModel):
    filename: str
    sha256: str
    width: int
    height: int
    dtype: str
    values: list[LossyValue]
    warning: str = LOSSY_WARNING
    compatible: bool
    reason: str | None = None
    expected_width: int | None = None
    expected_height: int | None = None
    #: Class the importer proposes for foreground values.
    default_class_id: int | None = None
    default_class_name: str | None = None
    default_class_source: str | None = None
    #: True when the user must pick a class explicitly before committing.
    requires_class_choice: bool = False


def _incompatible(
    path: Path, filename: str, dataset: Dataset, decoded: Any, reason: str
) -> tuple[LossyPreview, None]:
    return (
        LossyPreview(
            filename=filename,
            sha256=sha256_of_file(path),
            width=decoded.width if decoded is not None else 0,
            height=decoded.height if decoded is not None else 0,
            dtype=decoded.dtype if decoded is not None else "",
            values=[],
            compatible=False,
            reason=reason,
            expected_width=dataset.width,
            expected_height=dataset.height,
        ),
        None,
    )


def preview_lossy_raster(
    path: Path,
    filename: str,
    dataset: Dataset,
    ontology: ClassOntology | None = None,
    config: ProjectConfig | None = None,
) -> tuple[LossyPreview, np.ndarray | None]:
    try:
        decoded = decode_image(path, display_name=filename)
    except ImageDecodeError as exc:
        return _incompatible(path, filename, dataset, None, exc.message)
    array = decoded.array
    if array.ndim == 3:
        # A colour label image is ambiguous; only a single channel is accepted.
        if (
            array.shape[2] >= 3
            and np.array_equal(array[:, :, 0], array[:, :, 1])
            and np.array_equal(array[:, :, 0], array[:, :, 2])
        ):
            array = array[:, :, 0]
        else:
            return _incompatible(
                path,
                filename,
                dataset,
                decoded,
                "colour label images are ambiguous; provide a single-channel index mask",
            )
    if (decoded.width, decoded.height) != (dataset.width, dataset.height):
        return _incompatible(
            path,
            filename,
            dataset,
            decoded,
            "Mask dimensions do not match the annotation grid. "
            f"Expected: {dataset.width} x {dataset.height}. "
            f"Received: {decoded.width} x {decoded.height}. No import was performed.",
        )
    if not np.issubdtype(array.dtype, np.integer):
        return _incompatible(
            path, filename, dataset, decoded, "label raster must have an integer dtype"
        )
    labels = array.astype(np.int64)
    default = find_default_import_class(ontology, config) if ontology is not None else None
    if default is not None:
        source = (
            "buffer class"
            if default.stable_key == "buffer" or default.name.strip().casefold() == "buffer"
            else "project default import class"
        )
    else:
        source = None
    values: list[LossyValue] = []
    for value in np.unique(labels):
        if int(value) == 0:
            continue
        mask = labels == value
        values.append(
            LossyValue(
                value=int(value),
                pixel_count=int(mask.sum()),
                component_count=len(split_components(mask)),
                suggested_class_id=default.class_id if default is not None else None,
            )
        )
    return (
        LossyPreview(
            filename=filename,
            sha256=sha256_of_file(path),
            width=decoded.width,
            height=decoded.height,
            dtype=decoded.dtype,
            values=values,
            compatible=True,
            expected_width=dataset.width,
            expected_height=dataset.height,
            default_class_id=default.class_id if default is not None else None,
            default_class_name=default.name if default is not None else None,
            default_class_source=source,
            requires_class_choice=default is None and bool(values),
        ),
        labels,
    )


class LossyCommitResult(BaseModel):
    dataset_revision: int
    region_ids: list[str] = Field(default_factory=list)
    skipped_values: list[int] = Field(default_factory=list)
    #: raster value -> class id actually used
    mapping_used: dict[int, int] = Field(default_factory=dict)
    #: Class to activate after the import (the most-used target class).
    active_class_id: int | None = None
    imported_pixels: int = 0
    #: Union bounding box of the imported regions, ``[x, y, w, h]``.
    bbox_xywh: list[int] = Field(default_factory=lambda: [0, 0, 0, 0])
    warning: str = LOSSY_WARNING
    summary: str = ""


def commit_lossy_raster(
    regions: RegionService,
    project_id: str,
    dataset: Dataset,
    labels: np.ndarray,
    *,
    sha256: str,
    filename: str,
    value_to_class: dict[int, int | None],
    split: bool,
    confirm_discard: bool,
    expected_revision: int | None,
    actor: str | None,
    ontology_class_ids: set[int],
) -> LossyCommitResult:
    present = sorted(int(v) for v in np.unique(labels) if int(v) != 0)
    missing = [v for v in present if v not in value_to_class]
    if missing:
        raise AppError(
            "VALIDATION_ERROR",
            f"raster value(s) {missing} have no mapping; choose a class or mark them "
            "'do not import' explicitly",
            status_code=422,
            target={"dataset_id": dataset.dataset_id},
            details={"unmapped_values": missing},
        )
    discarded = [v for v in present if value_to_class.get(v) is None]
    if discarded and not confirm_discard:
        raise AppError(
            "VALIDATION_ERROR",
            f"raster value(s) {discarded} would not be imported; confirm the discard explicitly",
            status_code=409,
            target={"dataset_id": dataset.dataset_id},
            recovery="tick 'I confirm these values are not imported' or map them to a class",
            details={"discarded_values": discarded},
        )
    for value, class_id in value_to_class.items():
        if class_id is not None and class_id not in ontology_class_ids:
            raise AppError(
                "CLASS_REFERENCE_INVALID",
                f"value {value}: class {class_id} does not exist",
                status_code=422,
                target={"dataset_id": dataset.dataset_id},
            )
    region_ids: list[str] = []
    skipped: list[int] = []
    mapping_used: dict[int, int] = {}
    union = np.zeros(labels.shape, dtype=bool)
    class_pixels: dict[int, int] = {}

    def fn(region_set: RegionSet, current: Dataset) -> dict[str, Any]:
        for value in present:
            class_id = value_to_class.get(value)
            if class_id is None:
                skipped.append(value)
                continue
            mapping_used[value] = class_id
            mask = labels == value
            union[mask] = True
            class_pixels[class_id] = class_pixels.get(class_id, 0) + int(mask.sum())
            parts = split_components(mask) if split else [mask]
            for index, part in enumerate(parts):
                region = RegionService._new_region(
                    region_set,
                    current,
                    class_id,
                    part,
                    source="lossy_import",
                    provenance={
                        "importer_version": IMPORTER_VERSION,
                        "file": filename,
                        "file_sha256": sha256,
                        "dimensions": [int(labels.shape[1]), int(labels.shape[0])],
                        "raster_value": value,
                        "component_index": index,
                        "lossy_flattened_import": True,
                        "lossy_warning": LOSSY_WARNING,
                    },
                    name=None,
                )
                region_ids.append(region.region_id)
        return {
            "region_ids": region_ids,
            "skipped_values": skipped,
            "file": filename,
            "file_sha256": sha256,
            "dimensions": [int(labels.shape[1]), int(labels.shape[0])],
            "source_values": present,
            "mapping_used": {str(k): v for k, v in mapping_used.items()},
            "importer_version": IMPORTER_VERSION,
            "lossy_flattened_import": True,
        }

    updated, _ = regions.mutate(
        project_id,
        dataset.dataset_id,
        expected_revision,
        fn,
        operation="regions.import_lossy",
        actor=actor,
        source="lossy_import",
        region_ids=region_ids,
        class_ids=sorted(set(mapping_used.values())),
        provenance={"importer_version": IMPORTER_VERSION, "file_sha256": sha256},
    )
    active = max(class_pixels, key=lambda cid: class_pixels[cid]) if class_pixels else None
    summary = (
        f"Imported {len(region_ids)} region(s) from {filename} "
        f"({int(union.sum())} px, {len(mapping_used)} value(s))"
        + (f"; {len(skipped)} value(s) not imported" if skipped else "")
    )
    return LossyCommitResult(
        dataset_revision=updated.revision,
        region_ids=region_ids,
        skipped_values=skipped,
        mapping_used=mapping_used,
        active_class_id=active,
        imported_pixels=int(union.sum()),
        bbox_xywh=bounding_box(union),
        summary=summary,
    )
