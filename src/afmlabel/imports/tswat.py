# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""TSWAT/CVAT XML import service.

Two steps, like paired-image import: ``stage`` parses the file and returns
a report (which XML images match which datasets, label -> class mapping
proposals, problems); ``commit`` creates one region per XML shape with
full provenance. Nothing is flattened, no scientific precedence is
decided: every imported overlap starts as ``unreviewed``.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from afmlabel.annotations.service import RegionService
from afmlabel.domain.common import new_id, utc_now_iso
from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import (
    ClassDefinition,
    ClassOntology,
    HistoryEntry,
    Project,
    normalize_class_name,
    stable_key_from_name,
)
from afmlabel.domain.region import RegionSet
from afmlabel.errors import AppError, NotFoundError
from afmlabel.imports.cvat_xml import (
    IMPORTER_VERSION,
    CvatXmlError,
    ParsedAnnotations,
    parse_cvat_xml,
)
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import atomic_write_bytes, load_model, save_model
from afmlabel.persistence.rle import polygon_to_mask
from afmlabel.projects.datasets import DatasetStore
from afmlabel.projects.sanitize import safe_filename
from afmlabel.projects.store import ProjectStore

logger = logging.getLogger(__name__)

_DEFAULT_COLORS = [
    "#fa3253",
    "#3d3df5",
    "#b83df5",
    "#fa32b7",
    "#fafa37",
    "#fa7dbb",
    "#33ddff",
    "#ff6037",
    "#32b7fa",
    "#83e070",
    "#00a398",
    "#faad14",
]

ImageStatus = Literal["matched", "unmatched", "dimension_mismatch", "no_shapes", "duplicate"]
LabelProposal = Literal["existing", "create"]


class ImageMatch(BaseModel):
    image_name: str
    basename: str
    width: int
    height: int
    status: ImageStatus
    dataset_id: str | None = None
    dataset_name: str | None = None
    matched_modality: str | None = None
    shape_count: int
    mask_count: int
    polygon_count: int
    box_count: int
    skipped: list[str] = Field(default_factory=list)
    message: str | None = None


class LabelMapping(BaseModel):
    label: str
    color: str | None
    shape_count: int
    proposal: LabelProposal
    class_id: int | None = None
    class_name: str | None = None


class TswatStageReport(BaseModel):
    staged_id: str
    filename: str
    sha256: str
    importer_version: str
    format_version: str | None
    images: list[ImageMatch]
    labels: list[LabelMapping]
    warnings: list[str] = Field(default_factory=list)
    committable_dataset_count: int
    total_shape_count: int


class DatasetImportResult(BaseModel):
    dataset_id: str
    dataset_name: str
    image_names: list[str]
    region_ids: list[str]
    dataset_revision: int
    warnings: list[str] = Field(default_factory=list)


class TswatCommitReport(BaseModel):
    staged_id: str
    sha256: str
    importer_version: str
    datasets: list[DatasetImportResult]
    created_classes: list[dict[str, Any]]
    skipped_images: list[ImageMatch]
    warnings: list[str]
    project_revision: int


class StagedTswat(BaseModel):
    staged_id: str
    project_id: str
    filename: str
    sha256: str
    created_at: str


class TswatImportService:
    def __init__(
        self,
        workspace_root: Path,
        projects: ProjectStore,
        datasets: DatasetStore,
        regions: RegionService,
    ) -> None:
        self._root = paths.uploads_tmp_dir(workspace_root) / "tswat"
        self._projects = projects
        self._datasets = datasets
        self._regions = regions
        self._root.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------------- staging

    def _dir(self, staged_id: str) -> Path:
        if (
            not staged_id.startswith("tswat_")
            or "/" in staged_id
            or "\\" in staged_id
            or ".." in staged_id
        ):
            raise NotFoundError("TSWAT_IMPORT_INVALID", f"unknown staged import {staged_id}")
        return self._root / staged_id

    def stage(
        self, project_id: str, filename: str, data: bytes
    ) -> tuple[StagedTswat, ParsedAnnotations]:
        try:
            parsed = parse_cvat_xml(data)
        except CvatXmlError as exc:
            raise AppError(
                "TSWAT_IMPORT_INVALID",
                f"{filename}: {exc}",
                status_code=422,
                target={"project_id": project_id, "filename": filename},
                recovery="export the annotations from CVAT as 'CVAT for images 1.1' XML",
            ) from exc
        staged = StagedTswat(
            staged_id=new_id("tswat"),
            project_id=project_id,
            filename=safe_filename(filename, default="annotations.xml"),
            sha256=parsed.sha256,
            created_at=utc_now_iso(),
        )
        directory = self._dir(staged.staged_id)
        directory.mkdir(parents=True, exist_ok=False)
        atomic_write_bytes(directory / "annotations.xml", data, fsync=False)
        save_model(directory / "staged.json", staged, fsync=False)
        return staged, parsed

    def load_staged(self, project_id: str, staged_id: str) -> tuple[StagedTswat, ParsedAnnotations]:
        directory = self._dir(staged_id)
        meta_path = directory / "staged.json"
        if not meta_path.exists():
            raise NotFoundError(
                "TSWAT_IMPORT_INVALID",
                f"staged import {staged_id} does not exist or expired",
                target={"staged_id": staged_id},
                recovery="upload the XML again",
            )
        staged = load_model(meta_path, StagedTswat)
        if staged.project_id != project_id:
            raise NotFoundError(
                "TSWAT_IMPORT_INVALID", f"staged import {staged_id} belongs to another project"
            )
        parsed = parse_cvat_xml((directory / "annotations.xml").read_bytes())
        return staged, parsed

    def discard(self, staged_id: str) -> None:
        directory = self._dir(staged_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    # --------------------------------------------------------------- matching

    def _dataset_index(self, project_id: str) -> dict[str, tuple[Dataset, str]]:
        index: dict[str, tuple[Dataset, str]] = {}
        for summary in self._datasets.list_datasets(project_id):
            dataset = self._datasets.get(project_id, summary.dataset_id)
            for source in dataset.channels:
                key = source.original_filename.casefold()
                index.setdefault(key, (dataset, source.channel))
        return index

    def build_report(
        self,
        project_id: str,
        staged: StagedTswat,
        parsed: ParsedAnnotations,
        *,
        target_dataset_id: str | None = None,
        class_mapping: dict[str, str] | None = None,
    ) -> TswatStageReport:
        ontology = self._projects.get_ontology(project_id)
        index = self._dataset_index(project_id)
        warnings: list[str] = []
        images: list[ImageMatch] = []
        seen_datasets: dict[str, list[str]] = {}
        annotated = [img for img in parsed.images if img.shapes]
        for image in parsed.images:
            match = ImageMatch(
                image_name=image.name,
                basename=image.basename,
                width=image.width,
                height=image.height,
                status="matched",
                shape_count=len(image.shapes),
                mask_count=sum(1 for s in image.shapes if s.kind == "mask"),
                polygon_count=sum(1 for s in image.shapes if s.kind == "polygon"),
                box_count=sum(1 for s in image.shapes if s.kind == "box"),
                skipped=list(image.skipped),
            )
            hit = index.get(image.basename.casefold())
            if hit is None and target_dataset_id and len(annotated) == 1 and image.shapes:
                try:
                    hit = (self._datasets.get(project_id, target_dataset_id), "explicit")
                except AppError:
                    hit = None
            if not image.shapes:
                match.status = "no_shapes"
                match.message = "no region shapes on this image"
            elif hit is None:
                match.status = "unmatched"
                match.message = "no dataset has a source file with this name"
            else:
                dataset, modality = hit
                match.dataset_id = dataset.dataset_id
                match.dataset_name = dataset.name
                match.matched_modality = modality
                if (image.width, image.height) != (dataset.width, dataset.height):
                    match.status = "dimension_mismatch"
                    match.message = (
                        f"XML image is {image.width}x{image.height} but dataset {dataset.name} is "
                        f"{dataset.width}x{dataset.height}; annotations cannot be resampled"
                    )
                else:
                    seen_datasets.setdefault(dataset.dataset_id, []).append(image.name)
            images.append(match)
        for dataset_id, names in seen_datasets.items():
            if len(names) > 1:
                warnings.append(
                    f"dataset {dataset_id} receives shapes from {len(names)} XML images "
                    f"({', '.join(names)}); all channels share one annotation grid"
                )

        counts: dict[str, int] = {}
        colors: dict[str, str | None] = {label.name: label.color for label in parsed.labels}
        for image in parsed.images:
            for shape in image.shapes:
                counts[shape.label] = counts.get(shape.label, 0) + 1
        labels: list[LabelMapping] = []
        by_name = {normalize_class_name(c.name): c for c in ontology.classes}
        for name in parsed.label_names():
            override = (class_mapping or {}).get(name)
            existing = by_name.get(normalize_class_name(name))
            mapping = LabelMapping(
                label=name,
                color=colors.get(name),
                shape_count=counts.get(name, 0),
                proposal="existing" if existing else "create",
                class_id=existing.class_id if existing else None,
                class_name=existing.name if existing else name,
            )
            if override and override not in ("create", "skip"):
                cls = ontology.by_id(int(override)) if override.isdigit() else None
                if cls is None:
                    warnings.append(
                        f"label {name!r}: class {override} does not exist; proposal kept"
                    )
                else:
                    mapping.proposal = "existing"
                    mapping.class_id = cls.class_id
                    mapping.class_name = cls.name
            elif override == "create":
                mapping.proposal = "create"
                mapping.class_id = None
                mapping.class_name = name
            labels.append(mapping)

        committable = {m.dataset_id for m in images if m.status == "matched"}
        return TswatStageReport(
            staged_id=staged.staged_id,
            filename=staged.filename,
            sha256=staged.sha256,
            importer_version=parsed.importer_version,
            format_version=parsed.version,
            images=images,
            labels=labels,
            warnings=warnings,
            committable_dataset_count=len(committable),
            total_shape_count=sum(m.shape_count for m in images if m.status == "matched"),
        )

    @staticmethod
    def _make_dataset_importer(
        entries: list[tuple[ImageMatch, Any]],
        region_ids: list[str],
        dataset_warnings: list[str],
        skip_labels: set[str],
        label_to_class: dict[str, int],
        staged: StagedTswat,
    ) -> Callable[[RegionSet, Dataset], dict[str, Any]]:
        def fn(region_set: RegionSet, dataset: Dataset) -> dict[str, Any]:
            ordered: list[tuple[int, str, int, Any, Any]] = []
            for match, image in entries:
                for shape in image.shapes:
                    ordered.append((shape.z_order, match.image_name, shape.index, image, shape))
            # Source ordering becomes z-order: CVAT draws higher z_order and
            # later document order on top.
            ordered.sort(key=lambda item: (item[0], item[1], item[2]))
            for _z, image_name, _index, _image, shape in ordered:
                if shape.label in skip_labels or shape.label not in label_to_class:
                    dataset_warnings.append(
                        f"{image_name}: {shape.kind} #{shape.index} ({shape.label}) skipped"
                    )
                    continue
                if shape.kind == "polygon":
                    mask = polygon_to_mask(shape.points, dataset.height, dataset.width)
                else:
                    mask = np.asarray(shape.mask, dtype=bool)
                if not mask.any():
                    dataset_warnings.append(
                        f"{image_name}: {shape.kind} #{shape.index} ({shape.label}) is empty; "
                        "skipped"
                    )
                    continue
                region = RegionService._new_region(
                    region_set,
                    dataset,
                    label_to_class[shape.label],
                    mask,
                    source="tswat_import",
                    provenance={
                        "importer_version": IMPORTER_VERSION,
                        "file": staged.filename,
                        "file_sha256": staged.sha256,
                        "xml_image": image_name,
                        "xml_shape": shape.kind,
                        "xml_index": shape.index,
                        "xml_z_order": shape.z_order,
                        "xml_label": shape.label,
                        "xml_source": shape.source,
                        "xml_occluded": shape.occluded,
                        "xml_attributes": shape.attributes,
                        "warnings": shape.warnings,
                    },
                    name=None,
                )
                region_ids.append(region.region_id)
                dataset_warnings.extend(
                    f"{image_name}: {shape.kind} #{shape.index}: {w}" for w in shape.warnings
                )
            return {"region_ids": region_ids, "file_sha256": staged.sha256}

        return fn

    # ----------------------------------------------------------------- commit

    def commit(
        self,
        project_id: str,
        staged_id: str,
        *,
        expected_project_revision: int,
        class_mapping: dict[str, str],
        target_dataset_id: str | None,
        actor: str | None,
    ) -> TswatCommitReport:
        staged, parsed = self.load_staged(project_id, staged_id)
        report = self.build_report(
            project_id,
            staged,
            parsed,
            target_dataset_id=target_dataset_id,
            class_mapping=class_mapping,
        )
        skip_labels = {label for label, choice in class_mapping.items() if choice == "skip"}
        created_classes: list[dict[str, Any]] = []
        label_to_class: dict[str, int] = {}

        def mutate(project: Project, ontology: ClassOntology) -> None:
            for mapping in report.labels:
                if mapping.label in skip_labels:
                    continue
                if mapping.proposal == "existing" and mapping.class_id is not None:
                    label_to_class[mapping.label] = mapping.class_id
                    continue
                class_id = ontology.next_class_id
                keys = {c.stable_key for c in ontology.classes}
                key = stable_key_from_name(mapping.label)
                suffix = 2
                base = key
                while key in keys:
                    key = f"{base}_{suffix}"
                    suffix += 1
                color = mapping.color or _DEFAULT_COLORS[(class_id - 1) % len(_DEFAULT_COLORS)]
                ontology.classes.append(
                    ClassDefinition(
                        class_id=class_id,
                        stable_key=key,
                        name=mapping.label,
                        display_color=color,
                        description=f"imported from {staged.filename}",
                    )
                )
                ontology.next_class_id = class_id + 1
                label_to_class[mapping.label] = class_id
                created_classes.append(
                    {"class_id": class_id, "name": mapping.label, "color": color}
                )
            project.import_history.append(
                HistoryEntry(
                    kind="import_tswat",
                    entry_id=staged.staged_id,
                    at=utc_now_iso(),
                    summary=f"imported {staged.filename} ({report.total_shape_count} shapes)",
                    details={
                        "sha256": staged.sha256,
                        "importer_version": IMPORTER_VERSION,
                        "created_classes": [c["class_id"] for c in created_classes],
                    },
                )
            )

        project, ontology = self._projects.mutate_project(
            project_id,
            expected_project_revision,
            mutate,
            operation="import.tswat",
            actor=actor,
            source="tswat_import",
            details={"staged_id": staged_id, "sha256": staged.sha256, "filename": staged.filename},
        )

        results: list[DatasetImportResult] = []
        warnings = list(report.warnings)
        by_dataset: dict[str, list[tuple[ImageMatch, Any]]] = {}
        parsed_by_name = {img.name: img for img in parsed.images}
        for match in report.images:
            if match.status == "matched" and match.dataset_id:
                by_dataset.setdefault(match.dataset_id, []).append(
                    (match, parsed_by_name[match.image_name])
                )

        for dataset_id, entries in by_dataset.items():
            region_ids: list[str] = []
            dataset_warnings: list[str] = []

            fn = self._make_dataset_importer(
                entries, region_ids, dataset_warnings, skip_labels, label_to_class, staged
            )

            dataset, _ = self._regions.mutate(
                project_id,
                dataset_id,
                None,
                fn,
                operation="regions.import_tswat",
                actor=actor,
                source="tswat_import",
                region_ids=region_ids,
                provenance={"importer_version": IMPORTER_VERSION, "file_sha256": staged.sha256},
            )
            results.append(
                DatasetImportResult(
                    dataset_id=dataset_id,
                    dataset_name=dataset.name,
                    image_names=[m.image_name for m, _ in entries],
                    region_ids=region_ids,
                    dataset_revision=dataset.revision,
                    warnings=dataset_warnings,
                )
            )
        self.discard(staged_id)
        return TswatCommitReport(
            staged_id=staged_id,
            sha256=staged.sha256,
            importer_version=IMPORTER_VERSION,
            datasets=results,
            created_classes=created_classes,
            skipped_images=[m for m in report.images if m.status != "matched"],
            warnings=warnings,
            project_revision=project.revision,
        )
