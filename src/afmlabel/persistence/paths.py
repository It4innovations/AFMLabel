# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Workspace layout. All paths are derived here, nowhere else."""

from __future__ import annotations

from pathlib import Path

PROJECT_FILE = "project.json"
CLASSES_FILE = "classes.json"
UI_STATE_FILE = "ui_state.json"
DATASET_FILE = "dataset.json"
REGIONS_FILE = "regions.json"
VALIDATION_FILE = "latest.json"
EVENTS_FILE = "events.jsonl"
LOCK_FILE = "project.lock"


def projects_root(workspace_root: Path) -> Path:
    return workspace_root / "projects"


def project_dir(workspace_root: Path, project_id: str) -> Path:
    return projects_root(workspace_root) / project_id


def project_file(pdir: Path) -> Path:
    return pdir / PROJECT_FILE


def classes_file(pdir: Path) -> Path:
    return pdir / CLASSES_FILE


def ui_state_file(pdir: Path) -> Path:
    return pdir / UI_STATE_FILE


def datasets_dir(pdir: Path) -> Path:
    return pdir / "datasets"


def dataset_dir(pdir: Path, dataset_id: str) -> Path:
    return datasets_dir(pdir) / dataset_id


def dataset_file(ddir: Path) -> Path:
    return ddir / DATASET_FILE


def dataset_source_dir(ddir: Path) -> Path:
    return ddir / "source"


def dataset_annotations_dir(ddir: Path) -> Path:
    return ddir / "annotations"


def regions_file(ddir: Path) -> Path:
    return dataset_annotations_dir(ddir) / REGIONS_FILE


def dataset_imports_dir(ddir: Path) -> Path:
    return ddir / "imports"


def dataset_cache_dir(ddir: Path) -> Path:
    return ddir / "cache"


def dataset_jobs_dir(ddir: Path) -> Path:
    return ddir / "jobs"


def dataset_validation_dir(ddir: Path) -> Path:
    return ddir / "validation"


def validation_file(ddir: Path) -> Path:
    return dataset_validation_dir(ddir) / VALIDATION_FILE


def logs_dir(pdir: Path) -> Path:
    return pdir / "logs"


def events_file(pdir: Path) -> Path:
    return logs_dir(pdir) / EVENTS_FILE


def exports_dir(pdir: Path) -> Path:
    return pdir / "exports"


def locks_dir(pdir: Path) -> Path:
    return pdir / "locks"


def lock_file(pdir: Path) -> Path:
    return locks_dir(pdir) / LOCK_FILE


def auth_dir(workspace_root: Path) -> Path:
    return workspace_root / "auth"


def account_file(workspace_root: Path) -> Path:
    return auth_dir(workspace_root) / "account.json"


def sessions_file(workspace_root: Path) -> Path:
    return auth_dir(workspace_root) / "sessions.json"


def trash_dir(workspace_root: Path) -> Path:
    return workspace_root / "trash"


def uploads_tmp_dir(workspace_root: Path) -> Path:
    return workspace_root / "tmp" / "uploads"


def application_log_file(workspace_root: Path) -> Path:
    return workspace_root / "logs" / "application.log"
