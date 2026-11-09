# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Export profile interface and the shared exact representation.

Every profile receives :class:`ExactDataset` objects: the composed final
label map (int32, class ids, exactly one per pixel), the class ontology,
the source pair metadata, the validation report and provenance summary.
Profiles only *transform* this representation; canonical project state is
never touched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
from pydantic import BaseModel, Field

from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import ClassOntology, Project
from afmlabel.domain.region import RegionSet
from afmlabel.domain.validation import ValidationReport
from afmlabel.overlaps.engine import Composition

BoundaryExportPolicy = Literal["class", "zero"]
MaskSemantics = Literal["class", "instance"]
RasterFormat = Literal["tiff", "png"]


class ExportOptions(BaseModel):
    boundary_policy: BoundaryExportPolicy = "class"
    mask_semantics: MaskSemantics = "class"
    #: Include canonical regions.json copies (native profile only).
    include_regions: bool = True
    #: Machine-readable mask container: uncompressed TIFF (NanoUNet
    #: default) or PNG. Values are uint16 scalars in both cases.
    raster_format: RasterFormat = "tiff"
    #: Also write a human-visible RGB mask using the exact class colours.
    color_visualization: bool = True


@dataclass
class ExactDataset:
    dataset: Dataset
    region_set: RegionSet
    composition: Composition
    validation: ValidationReport
    label_map: np.ndarray  # int32 class ids after Boundary policy, 0 only via "zero"
    instance_map: np.ndarray  # int32 unique id per winning region, 0 only via "zero"
    instance_table: list[dict[str, Any]] = field(default_factory=list)
    export_stem: str = ""


@dataclass
class ExportContext:
    project: Project
    ontology: ClassOntology
    options: ExportOptions
    export_id: str
    created_at: str
    app_version: str
    #: ``(dataset_id, channel_key) -> source file``.
    source_paths: dict[tuple[str, str], Path] = field(default_factory=dict)
    #: ``channel_key -> export folder / file label`` (``Topography``, ``Friction``, ...).
    channel_folders: dict[str, str] = field(default_factory=dict)
    #: Export value -> colour legend shared by every profile (see rasters.py).
    color_legend: list[dict[str, Any]] = field(default_factory=list)


class DatasetExportEntry(BaseModel):
    dataset_id: str
    dataset_name: str
    export_stem: str
    pairing_key: str = ""
    #: Original upload filenames the stem was derived from.
    original_filenames: dict[str, str] = Field(default_factory=dict)
    width: int
    height: int
    dataset_revision: int
    validated_revision: int
    composition_sha256: str
    files: dict[str, str] = Field(default_factory=dict)
    #: ``{channel_key: sha256}`` of every source image of the dataset.
    source_sha256: dict[str, str] = Field(default_factory=dict)
    #: Legacy two-channel fields (kept so older manifests/records still load).
    topography_sha256: str | None = None
    friction_sha256: str | None = None
    region_count: int
    class_pixel_histogram: dict[int, int] = Field(default_factory=dict)
    overlap_groups: int
    overlap_review_summary: dict[str, int] = Field(default_factory=dict)
    boundary_fill_used: bool
    provenance_summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ExportProfile(Protocol):
    profile_id: str
    display_name: str

    def write(
        self, root: Path, context: ExportContext, datasets: list[ExactDataset]
    ) -> list[DatasetExportEntry]:
        """Write the profile's files under ``root`` and describe them."""
        ...
