# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Export service: builds the exact representation, applies a profile,
packages the result and records history."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from afmlabel import __version__
from afmlabel.annotations.service import RegionService
from afmlabel.domain.common import new_id, utc_now_iso
from afmlabel.domain.project import ClassOntology, HistoryEntry, Project, SemanticRole
from afmlabel.errors import AppError, NotFoundError
from afmlabel.exports.base import (
    DatasetExportEntry,
    ExactDataset,
    ExportContext,
    ExportOptions,
    ExportProfile,
)
from afmlabel.exports.nanounet import NanoUNetExport, channel_folder
from afmlabel.exports.native_exact import NativeExactExport
from afmlabel.exports.rasters import color_legend, export_stems, safe_stem
from afmlabel.images.display import DEFAULT_DISPLAY
from afmlabel.overlaps.engine import compose
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_model, save_model
from afmlabel.projects.datasets import DatasetStore
from afmlabel.projects.store import ProjectStore
from afmlabel.validation.service import ValidationService

PROFILES: dict[str, ExportProfile] = {
    NativeExactExport.profile_id: NativeExactExport(),
    NanoUNetExport.profile_id: NanoUNetExport(),
}


class SkippedDataset(BaseModel):
    dataset_id: str
    dataset_name: str
    code: str
    reason: str


class ExportRecord(BaseModel):
    export_id: str
    project_id: str
    profile_id: str
    created_at: str
    app_version: str
    options: ExportOptions
    datasets: list[DatasetExportEntry] = Field(default_factory=list)
    skipped: list[SkippedDataset] = Field(default_factory=list)
    package_filename: str
    package_sha256: str | None = None
    package_size_bytes: int | None = None
    class_mapping: list[dict[str, Any]] = Field(default_factory=list)
    actor: str | None = None


def export_id_mapping(ontology: ClassOntology) -> dict[int, int]:
    return {
        c.class_id: (c.export_id if c.export_id is not None else c.class_id)
        for c in ontology.classes
    }


class ExportService:
    def __init__(
        self,
        projects: ProjectStore,
        datasets: DatasetStore,
        regions: RegionService,
        validation: ValidationService,
    ) -> None:
        self._projects = projects
        self._datasets = datasets
        self._regions = regions
        self._validation = validation

    # ---------------------------------------------------------------- reads

    def exports_dir(self, project_id: str) -> Path:
        return paths.exports_dir(self._projects.project_dir(project_id))

    def _record_path(self, project_id: str, export_id: str) -> Path:
        if (
            not export_id.startswith("exp_")
            or "/" in export_id
            or "\\" in export_id
            or ".." in export_id
        ):
            raise NotFoundError("EXPORT_NOT_FOUND", f"export {export_id} does not exist")
        return self.exports_dir(project_id) / export_id / "export.json"

    def get(self, project_id: str, export_id: str) -> ExportRecord:
        path = self._record_path(project_id, export_id)
        if not path.exists():
            raise NotFoundError(
                "EXPORT_NOT_FOUND",
                f"export {export_id} does not exist",
                target={"project_id": project_id, "export_id": export_id},
            )
        return load_model(path, ExportRecord)

    def package_path(self, project_id: str, export_id: str) -> Path:
        record = self.get(project_id, export_id)
        return self.exports_dir(project_id) / export_id / record.package_filename

    def list_exports(self, project_id: str) -> list[ExportRecord]:
        root = self.exports_dir(project_id)
        records: list[ExportRecord] = []
        if root.exists():
            for entry in root.iterdir():
                path = entry / "export.json"
                if path.exists():
                    try:
                        records.append(load_model(path, ExportRecord))
                    except (OSError, ValueError):
                        continue
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def delete(self, project_id: str, export_id: str, *, actor: str | None) -> None:
        self.get(project_id, export_id)
        shutil.rmtree(self.exports_dir(project_id) / export_id, ignore_errors=True)
        self._projects.record_event(
            project_id,
            operation="export.delete",
            source="export",
            actor=actor,
            details={"export_id": export_id},
        )

    # ---------------------------------------------------------------- build

    def _exact(
        self, project_id: str, dataset_id: str, ontology: ClassOntology, options: ExportOptions
    ) -> tuple[ExactDataset | None, SkippedDataset | None]:
        dataset, region_set = self._regions.load(project_id, dataset_id)
        report = self._validation.latest_report(project_id, dataset_id)
        if report is None:
            return None, SkippedDataset(
                dataset_id=dataset_id,
                dataset_name=dataset.name,
                code="VALIDATION_MISSING",
                reason="dataset has never been validated",
            )
        if (
            report.dataset_revision != dataset.revision
            or dataset.validated_revision != dataset.revision
        ):
            return None, SkippedDataset(
                dataset_id=dataset_id,
                dataset_name=dataset.name,
                code="VALIDATION_STALE",
                reason=(
                    f"validated at r{report.dataset_revision}, dataset is at r{dataset.revision}"
                ),
            )
        if report.status != "valid":
            return None, SkippedDataset(
                dataset_id=dataset_id,
                dataset_name=dataset.name,
                code="VALIDATION_FAILED",
                reason="latest validation is blocked: "
                + ", ".join(i.code for i in report.blocking),
            )
        composition = compose(region_set, ontology, dataset.height, dataset.width)
        if composition.sha256() != report.composition_sha256:
            return None, SkippedDataset(
                dataset_id=dataset_id,
                dataset_name=dataset.name,
                code="VALIDATION_STALE",
                reason="composition differs from the validated one; validate again",
            )
        if (composition.label_map == 0).any():
            return None, SkippedDataset(
                dataset_id=dataset_id,
                dataset_name=dataset.name,
                code="UNLABELED_PIXELS",
                reason="composition contains unlabeled pixels",
            )
        mapping = export_id_mapping(ontology)
        boundary_ids = {
            c.class_id for c in ontology.classes if c.semantic_role == SemanticRole.BOUNDARY
        }
        label_map = np.zeros_like(composition.label_map)
        for class_id, export_id in mapping.items():
            label_map[composition.label_map == class_id] = export_id
        instance_map = np.zeros_like(composition.winner_map)
        table: list[dict[str, Any]] = []
        next_instance = 1
        for index, decoded in enumerate(composition.regions, start=1):
            wins = composition.winner_map == index
            if not wins.any():
                continue
            instance_map[wins] = next_instance
            table.append(
                {
                    "instance_id": next_instance,
                    "region_id": decoded.region.region_id,
                    "class_id": decoded.region.class_id,
                    "class_export_id": mapping[decoded.region.class_id],
                    "pixels": int(wins.sum()),
                    "source": decoded.region.source,
                }
            )
            next_instance += 1
        if options.boundary_policy == "zero" and boundary_ids:
            boundary_pixels = np.isin(composition.label_map, list(boundary_ids))
            label_map[boundary_pixels] = 0
            instance_map[boundary_pixels] = 0
        return (
            ExactDataset(
                dataset=dataset,
                region_set=region_set,
                composition=composition,
                validation=report,
                label_map=label_map,
                instance_map=instance_map,
                instance_table=table,
            ),
            None,
        )

    def run(
        self,
        project_id: str,
        *,
        profile_id: str,
        dataset_ids: list[str] | None,
        options: ExportOptions,
        actor: str | None,
    ) -> ExportRecord:
        profile = PROFILES.get(profile_id)
        if profile is None:
            raise AppError(
                "EXPORT_PROFILE_INVALID",
                f"unknown export profile {profile_id!r}",
                status_code=422,
                target={"project_id": project_id},
                recovery=f"choose one of {sorted(PROFILES)}",
            )
        project: Project = self._projects.get_project(project_id)
        ontology = self._projects.get_ontology(project_id)
        try:
            ontology.validate_unique()
        except ValueError as exc:
            raise AppError(
                "EXPORT_PROFILE_INVALID", f"class export mapping invalid: {exc}", status_code=422
            ) from exc
        selected = dataset_ids or list(project.dataset_order)
        unknown = [d for d in selected if d not in project.dataset_order]
        if unknown:
            raise AppError("DATASET_NOT_FOUND", f"unknown dataset ids: {unknown}", status_code=404)
        exacts: list[ExactDataset] = []
        skipped: list[SkippedDataset] = []
        for dataset_id in selected:
            exact, skip = self._exact(project_id, dataset_id, ontology, options)
            if exact is not None:
                exacts.append(exact)
            elif skip is not None:
                skipped.append(skip)
        if not exacts:
            raise AppError(
                "VALIDATION_STALE",
                "no dataset is validated at its current revision; nothing to export",
                status_code=409,
                target={"project_id": project_id},
                recovery="validate the datasets in the workspace first",
                details={"skipped": [s.model_dump() for s in skipped]},
            )
        stems = export_stems(
            [e.dataset.pairing_key for e in exacts],
            loader_id_matching=bool(getattr(profile, "loader_id_matching", False)),
        )
        for exact, stem in zip(exacts, stems, strict=True):
            exact.export_stem = stem

        export_id = new_id("exp")
        created_at = utc_now_iso()
        export_dir = self.exports_dir(project_id) / export_id
        staging = export_dir / "package"
        staging.mkdir(parents=True, exist_ok=False)
        mapping = export_id_mapping(ontology)
        legend = color_legend(ontology, mapping)
        context = ExportContext(
            project=project,
            ontology=ontology,
            options=options,
            export_id=export_id,
            created_at=created_at,
            app_version=__version__,
            source_paths={
                (e.dataset.dataset_id, channel): self._datasets.source_path(e.dataset, channel)
                for e in exacts
                for channel in e.dataset.channel_keys
            },
            channel_folders={
                c.key: channel_folder(c.key, c.label) for c in project.channels
            },
            color_legend=legend,
        )
        try:
            entries = profile.write(staging, context, exacts)
            class_mapping = [
                {
                    "class_id": c.class_id,
                    "export_id": c.export_id if c.export_id is not None else c.class_id,
                    "export_value": mapping[c.class_id],
                    "stable_key": c.stable_key,
                    "name": c.name,
                    "semantic_role": c.semantic_role.value,
                    "enabled": c.enabled,
                    "display_color": c.display_color,
                }
                for c in ontology.classes
            ]
            manifest = {
                "afmlabel_version": __version__,
                "export_id": export_id,
                "created_at": created_at,
                "project_id": project_id,
                "project_name": project.name,
                "project_revision": project.revision,
                "profile": profile_id,
                "options": options.model_dump(),
                "mask_value_semantics": (
                    "class export id"
                    if options.mask_semantics == "class"
                    else "instance id (see datasets[].instances)"
                ),
                "mask_raster_format": options.raster_format,
                "boundary_policy": options.boundary_policy,
                "unlabeled_pixels": "never present; 0 appears only under boundary_policy=zero",
                "image_normalisation": DEFAULT_DISPLAY.as_dict(),
                "classes": class_mapping,
                "color_legend": legend,
                "color_visualization": (
                    "files[mask_color]: RGB PNG rendered from the same final composition as the "
                    "machine mask; each pixel is exactly one legend colour (no anti-aliasing, "
                    "no alpha); 0 renders black"
                    if options.color_visualization
                    else "disabled"
                ),
                "naming": {
                    "basis": "pairing key derived from the original source filenames "
                    "(channel tokens removed), sanitised; identical stem in every directory",
                    "loader_id_matching": bool(getattr(profile, "loader_id_matching", False)),
                    "sequence_prefix_applied": any(
                        s != safe_stem(e.dataset.pairing_key) and "~" not in s
                        for s, e in zip(stems, exacts, strict=True)
                    ),
                },
                "datasets": [e.model_dump() for e in entries],
                "instances": {e.dataset.dataset_id: e.instance_table for e in exacts},
                "skipped": [s.model_dump() for s in skipped],
                "contract": "docs/user-guide.md#profiles"
                if profile_id == "nanounet"
                else "native exact",
            }
            (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            (staging / "classes.json").write_text(
                json.dumps(class_mapping, indent=2), encoding="utf-8"
            )
            package_name = f"afmlabel-{profile_id}-{export_id}.zip"
            package_path = export_dir / package_name
            with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(p for p in staging.rglob("*") if p.is_file()):
                    archive.write(path, path.relative_to(staging).as_posix())
            from afmlabel.images.decode import sha256_of_file

            record = ExportRecord(
                export_id=export_id,
                project_id=project_id,
                profile_id=profile_id,
                created_at=created_at,
                app_version=__version__,
                options=options,
                datasets=entries,
                skipped=skipped,
                package_filename=package_name,
                package_sha256=sha256_of_file(package_path),
                package_size_bytes=package_path.stat().st_size,
                class_mapping=class_mapping,
                actor=actor,
            )
            save_model(export_dir / "export.json", record)
        except BaseException:
            shutil.rmtree(export_dir, ignore_errors=True)
            raise
        shutil.rmtree(staging, ignore_errors=True)

        def mutate(current: Project, _: ClassOntology) -> None:
            current.export_history.append(
                HistoryEntry(
                    kind="export",
                    entry_id=export_id,
                    at=created_at,
                    summary=(
                        f"{profile_id}: {len(entries)} dataset(s) exported, {len(skipped)} skipped"
                    ),
                    details={
                        "profile": profile_id,
                        "dataset_ids": [e.dataset_id for e in entries],
                        "package_sha256": record.package_sha256,
                    },
                )
            )

        self._projects.mutate_project(
            project_id,
            self._projects.get_project(project_id).revision,
            mutate,
            operation="export.create",
            actor=actor,
            source="export",
            details={"export_id": export_id, "profile": profile_id, "datasets": len(entries)},
        )
        return record
