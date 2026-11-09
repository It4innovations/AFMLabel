# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Native exact export: everything needed to reproduce the
final mask and trace it back to sources, classes, regions and validation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from afmlabel.exports.base import DatasetExportEntry, ExactDataset, ExportContext
from afmlabel.exports.rasters import raster_suffix, write_color_mask, write_label_raster
from afmlabel.images.decode import sha256_of_file

PROFILE_ID = "native_exact"


def write_uint16_png(path: Path, array: np.ndarray) -> None:
    """Kept for existing callers; delegates to the shared writer."""
    write_label_raster(path, array, "png")


def provenance_summary(exact: ExactDataset) -> dict[str, Any]:
    sources: dict[str, int] = {}
    backends: set[str] = set()
    imports: set[str] = set()
    for region in exact.region_set.regions:
        sources[region.source] = sources.get(region.source, 0) + 1
        backend = region.provenance.get("backend_id")
        if backend:
            backends.add(str(backend))
        importer = region.provenance.get("importer_version")
        if importer:
            imports.add(str(importer))
    return {
        "region_sources": sources,
        "model_backends": sorted(backends),
        "importers": sorted(imports),
        "source_replacements": len(exact.dataset.replacements),
        "user_corrected_regions": sum(1 for r in exact.region_set.regions if r.user_corrected),
    }


def review_summary(exact: ExactDataset) -> dict[str, int]:
    summary: dict[str, int] = {}
    for review in exact.region_set.overlap_reviews.values():
        summary[review.state] = summary.get(review.state, 0) + 1
    return summary


def entry_for(exact: ExactDataset, files: dict[str, str]) -> DatasetExportEntry:
    validation = exact.validation
    return DatasetExportEntry(
        dataset_id=exact.dataset.dataset_id,
        dataset_name=exact.dataset.name,
        export_stem=exact.export_stem,
        pairing_key=exact.dataset.pairing_key,
        original_filenames={s.channel: s.original_filename for s in exact.dataset.channels},
        width=exact.dataset.width,
        height=exact.dataset.height,
        dataset_revision=exact.dataset.revision,
        validated_revision=validation.dataset_revision,
        composition_sha256=validation.composition_sha256 or "",
        files=files,
        source_sha256={s.channel: s.sha256 for s in exact.dataset.channels},
        topography_sha256=(
            exact.dataset.topography.sha256 if exact.dataset.has_channel("topography") else None
        ),
        friction_sha256=(
            exact.dataset.friction.sha256 if exact.dataset.has_channel("friction") else None
        ),
        region_count=len(exact.region_set.regions),
        class_pixel_histogram=validation.class_pixel_histogram,
        overlap_groups=validation.overlap_group_count,
        overlap_review_summary=review_summary(exact),
        boundary_fill_used=any(r.source == "boundary_fill" for r in exact.region_set.regions),
        provenance_summary=provenance_summary(exact),
        warnings=[f"{w.code}: {w.message}" for w in validation.warnings],
    )


class NativeExactExport:
    profile_id = PROFILE_ID
    display_name = "Native exact (masks + metadata + regions)"
    loader_id_matching = False

    def write(
        self, root: Path, context: ExportContext, datasets: list[ExactDataset]
    ) -> list[DatasetExportEntry]:
        entries: list[DatasetExportEntry] = []
        options = context.options
        suffix = raster_suffix(options.raster_format)
        for exact in datasets:
            ddir = root / "datasets" / (exact.export_stem or exact.dataset.dataset_id)
            ddir.mkdir(parents=True, exist_ok=True)
            files: dict[str, str] = {}
            mask_path = ddir / f"final_mask{suffix}"
            write_label_raster(mask_path, exact.label_map, options.raster_format)
            files["final_mask"] = str(mask_path.relative_to(root).as_posix())
            instance_path = ddir / f"instance_mask{suffix}"
            write_label_raster(instance_path, exact.instance_map, options.raster_format)
            files["instance_mask"] = str(instance_path.relative_to(root).as_posix())
            if options.color_visualization:
                color_path = ddir / "mask_color.png"
                write_color_mask(color_path, exact.label_map, context.color_legend)
                files["mask_color"] = str(color_path.relative_to(root).as_posix())
            for channel in exact.dataset.channel_keys:
                source = context.source_paths.get((exact.dataset.dataset_id, channel))
                if source is not None and source.exists():
                    target = ddir / f"{channel}{source.suffix.lower()}"
                    shutil.copy2(source, target)
                    files[f"{channel}_source"] = str(target.relative_to(root).as_posix())
            if options.include_regions:
                regions_path = ddir / "regions.json"
                regions_path.write_text(
                    exact.region_set.model_dump_json(indent=2), encoding="utf-8"
                )
                files["regions"] = str(regions_path.relative_to(root).as_posix())
            validation_path = ddir / "validation.json"
            validation_path.write_text(exact.validation.model_dump_json(indent=2), encoding="utf-8")
            files["validation"] = str(validation_path.relative_to(root).as_posix())
            entry = entry_for(exact, files)
            metadata = {
                "dataset": exact.dataset.model_dump(),
                "export": entry.model_dump(),
                "instances": exact.instance_table,
                "mask_encoding": {
                    "final_mask": (
                        f"uint16 {options.raster_format.upper()}, value = class export id "
                        "(0 only under boundary_policy=zero)"
                    ),
                    "instance_mask": (
                        f"uint16 {options.raster_format.upper()}, value = instance id from the "
                        "instances table"
                    ),
                    "mask_color": "RGB PNG, exact class display colours (see color_legend)",
                },
                "color_legend": context.color_legend,
                "file_sha256": {name: sha256_of_file(root / rel) for name, rel in files.items()},
            }
            (ddir / "dataset.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            files["metadata"] = str((ddir / "dataset.json").relative_to(root).as_posix())
            entry.files = files
            entries.append(entry)
        return entries
