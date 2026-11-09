# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import json
from pathlib import Path

import pytest

from afmlabel.domain.project import ClassDefinition, ClassOntology, Project, UiState
from afmlabel.errors import AppError, RevisionConflictError
from afmlabel.persistence import paths
from afmlabel.persistence.migrations import MigrationError, migrate, register_step
from afmlabel.projects.store import ProjectNotFoundError, ProjectStore


def test_create_list_get_delete(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create_project("  My   AFM\x00 project ")
    assert project.name == "My AFM project"
    assert project.revision == 1
    assert store.get_project(project.project_id) == project
    summaries = store.list_projects()
    assert [s.project_id for s in summaries] == [project.project_id]
    assert summaries[0].class_count == 0

    trash = store.delete_project(project.project_id, project.revision)
    assert trash.exists()
    assert store.list_projects() == []
    with pytest.raises(ProjectNotFoundError):
        store.get_project(project.project_id)


def test_empty_name_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AppError) as excinfo:
        ProjectStore(tmp_path).create_project("   ")
    assert excinfo.value.code == "PROJECT_INVALID"


def test_mutation_bumps_revision_and_records_event(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create_project("p")

    def add_class(_: Project, ontology: ClassOntology) -> None:
        ontology.classes.append(
            ClassDefinition(
                class_id=1, stable_key="layer_a", name="Layer A", display_color="#ff0000"
            )
        )
        ontology.next_class_id = 2

    updated, ontology = store.mutate_project(
        project.project_id, 1, add_class, operation="class.create", class_ids=[1]
    )
    assert updated.revision == 2
    assert ontology.classes[0].name == "Layer A"
    events = store.list_events(project.project_id)
    assert [e.operation for e in events] == ["project.create", "class.create"]
    assert events[-1].previous_revision == 1 and events[-1].new_revision == 2
    assert events[-1].class_ids == [1]


def test_revision_conflict_is_detected_and_nothing_is_written(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create_project("p")

    def rename(p: Project, _: ClassOntology) -> None:
        p.name = "renamed"

    with pytest.raises(RevisionConflictError) as excinfo:
        store.mutate_project(project.project_id, 99, rename, operation="project.update")
    assert excinfo.value.code == "REVISION_CONFLICT"
    assert excinfo.value.details == {"expected_revision": 99, "current_revision": 1}
    assert store.get_project(project.project_id).name == "p"


def test_failed_mutation_does_not_persist_partial_state(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create_project("p")

    def duplicate_names(_: Project, ontology: ClassOntology) -> None:
        ontology.classes = [
            ClassDefinition(class_id=1, stable_key="a", name="Same", display_color="#000000"),
            ClassDefinition(class_id=2, stable_key="b", name="same", display_color="#000001"),
        ]

    with pytest.raises(ValueError, match="unique"):
        store.mutate_project(project.project_id, 1, duplicate_names, operation="class.create")
    assert store.get_ontology(project.project_id).classes == []
    assert store.get_project(project.project_id).revision == 1


def test_project_survives_store_restart(tmp_path: Path) -> None:
    first = ProjectStore(tmp_path)
    project = first.create_project("persisted")
    first.save_ui_state(project.project_id, UiState(channel="friction", zoom=2.5))
    second = ProjectStore(tmp_path)
    assert second.get_project(project.project_id).name == "persisted"
    assert second.get_ui_state(project.project_id).channel == "friction"


def test_recover_incomplete_writes_removes_temp_files(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    project = store.create_project("p")
    pdir = store.project_dir(project.project_id)
    stale = pdir / ".project.json.xyz.tmp"
    stale.write_text("partial")
    removed = store.recover_incomplete_writes()
    assert removed == [stale]
    assert store.get_project(project.project_id).name == "p"


def test_unreadable_project_is_skipped_in_listing(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    good = store.create_project("good")
    bad = store.create_project("bad")
    paths.project_file(store.project_dir(bad.project_id)).write_text("{corrupt")
    assert [s.project_id for s in store.list_projects()] == [good.project_id]


def test_path_traversal_ids_are_rejected(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path)
    for bad in ("../x", "proj_../..", "..\\..", "etc"):
        with pytest.raises(ProjectNotFoundError):
            store.get_project(bad)


def test_schema_migration_interface(tmp_path: Path) -> None:
    with pytest.raises(MigrationError):
        migrate("project", {"schema_version": 999})
    with pytest.raises(MigrationError):
        migrate("project", {})

    # A registered step runs and stamps the new version.
    register_step("validation", 0)(lambda raw: {**raw, "migrated": True})
    result = migrate("validation", {"schema_version": 0})
    assert result["migrated"] is True and result["schema_version"] == 1

    store = ProjectStore(tmp_path)
    project = store.create_project("p")
    file = paths.project_file(store.project_dir(project.project_id))
    raw = json.loads(file.read_text())
    raw["schema_version"] = 999
    file.write_text(json.dumps(raw))
    with pytest.raises(MigrationError):
        store.get_project(project.project_id)
