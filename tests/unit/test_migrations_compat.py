# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Backward readability of persisted documents written before the upgrades.

The annotation-UX upgrade added only optional fields (schema versions
stayed at 1). The multi-channel upgrade bumped ``project`` and ``dataset``
to schema version 2 with registered migration steps, so v1 files written by
``0.2.0`` must still load and come out as the classic two-channel layout."""

from __future__ import annotations

import json
from pathlib import Path

from afmlabel.domain.dataset import Dataset
from afmlabel.domain.project import (
    ClassDefinition,
    ClassOntology,
    Project,
    ProjectConfig,
    UiState,
    find_default_import_class,
)
from afmlabel.domain.region import RegionSet
from afmlabel.exports.base import ExportOptions
from afmlabel.exports.service import ExportRecord
from afmlabel.persistence.migrations import migrate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema_v1"


def test_v1_project_without_default_import_class_key_loads() -> None:
    raw = json.loads((FIXTURES / "project.json").read_text(encoding="utf-8"))
    project = Project.model_validate(migrate("project", raw))
    assert project.schema_version == 2
    assert project.config.default_import_class_key is None
    assert project.config.boundary_mode == "explicit_class"
    # v1 projects always had the two classic channels.
    assert project.channel_keys == ["topography", "friction"]
    assert project.channels[0].label == "Topography"
    assert "topo" in project.channels[0].filename_tokens


def test_v1_dataset_migrates_to_channel_list() -> None:
    raw = json.loads((FIXTURES / "dataset.json").read_text(encoding="utf-8"))
    dataset = Dataset.model_validate(migrate("dataset", raw))
    assert dataset.schema_version == 2
    assert dataset.channel_keys == ["topography", "friction"]
    assert dataset.topography.original_filename == "239_topography.tiff"
    assert dataset.friction.sha256.startswith("b" * 8)
    assert dataset.source("friction").channel == "friction"
    assert dataset.replacements[0].channel == "friction"
    assert "modality" not in dataset.channels[0].model_dump()


def test_v1_ui_state_modality_field_is_read_as_channel() -> None:
    state = UiState.model_validate({"modality": "friction", "zoom": 2.0})
    assert state.channel == "friction"
    assert state.model_dump()["channel"] == "friction"
    assert "modality" not in state.model_dump()


def test_v1_regions_file_loads_and_keeps_reviews() -> None:
    raw = json.loads((FIXTURES / "regions.json").read_text(encoding="utf-8"))
    region_set = RegionSet.model_validate(migrate("regions", raw))
    assert [r.region_id for r in region_set.regions] == ["reg_000001", "reg_000002"]
    assert region_set.regions[1].locked is True
    assert region_set.overlap_reviews["ovl_abc123def456"].state == "reviewed_intentional"


def test_v1_export_record_without_new_options_loads_with_defaults() -> None:
    raw = json.loads((FIXTURES / "export.json").read_text(encoding="utf-8"))
    record = ExportRecord.model_validate(raw)
    assert record.options.raster_format == "tiff"
    assert record.options.color_visualization is True
    assert record.datasets[0].pairing_key == "" and record.datasets[0].original_filenames == {}
    assert ExportOptions().model_dump()["raster_format"] == "tiff"


def test_default_import_class_prefers_buffer_then_configured_key() -> None:
    ontology = ClassOntology(
        classes=[
            ClassDefinition(class_id=1, stable_key="slg", name="SLG", display_color="#ff0000"),
            ClassDefinition(
                class_id=2, stable_key="buffer", name="Buffer", display_color="#00ff00"
            ),
        ]
    )
    assert find_default_import_class(ontology).class_id == 2  # type: ignore[union-attr]
    ontology.classes[1] = ontology.classes[1].model_copy(update={"enabled": False})
    assert find_default_import_class(ontology) is None
    config = ProjectConfig(default_import_class_key="slg")
    assert find_default_import_class(ontology, config).class_id == 1  # type: ignore[union-attr]
    named = ClassOntology(
        classes=[
            ClassDefinition(class_id=5, stable_key="x", name="  buffer ", display_color="#000000")
        ]
    )
    assert find_default_import_class(named).class_id == 5  # type: ignore[union-attr]
