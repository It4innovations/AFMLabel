# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Filesystem project store.

Layout per project::

    workspace/projects/<project_id>/
        project.json      authoritative project state, carries ``revision``
        classes.json      class ontology (written before project.json)
        ui_state.json     browser resume state, last-writer-wins, no revision
        datasets/         one directory per dataset
        logs/events.jsonl append-only audit log
        exports/          export packages
        locks/project.lock

Every mutation goes through ``mutate_project`` which performs an
optimistic revision check under a per-project in-process lock, bumps the
revision, writes atomically and records an audit event.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from afmlabel.domain.common import new_id, utc_now_iso
from afmlabel.domain.events import Event, EventSource
from afmlabel.domain.project import (
    ChannelDefinition,
    ClassOntology,
    LockState,
    Project,
    ProjectSummary,
    UiState,
    default_channels,
    validate_channels,
)
from afmlabel.errors import AppError, NotFoundError, RevisionConflictError
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import (
    load_json,
    remove_stale_temp_files,
    save_model,
)
from afmlabel.persistence.jsonl import JsonlLog
from afmlabel.persistence.locks import ProjectLock
from afmlabel.persistence.migrations import migrate
from afmlabel.projects.sanitize import sanitize_display_name

logger = logging.getLogger(__name__)

_PROJECT_ID_PATTERN = "proj"


class ProjectNotFoundError(NotFoundError):
    def __init__(self, project_id: str) -> None:
        super().__init__(
            "PROJECT_NOT_FOUND",
            f"project {project_id} does not exist",
            target={"project_id": project_id},
            recovery="return to the project list",
        )


class ProjectStore:
    def __init__(
        self, workspace_root: Path, *, fsync: bool = True, lock_stale_seconds: float = 90.0
    ) -> None:
        self._root = workspace_root
        self._fsync = fsync
        self._lock_stale_seconds = lock_stale_seconds
        self._guards: dict[str, threading.Lock] = {}
        self._guards_lock = threading.Lock()
        paths.projects_root(self._root).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- paths

    @property
    def workspace_root(self) -> Path:
        return self._root

    def project_dir(self, project_id: str) -> Path:
        if (
            not project_id.startswith(f"{_PROJECT_ID_PATTERN}_")
            or "/" in project_id
            or "\\" in project_id
            or ".." in project_id
        ):
            raise ProjectNotFoundError(project_id)
        return paths.project_dir(self._root, project_id)

    def _guard(self, project_id: str) -> threading.Lock:
        with self._guards_lock:
            guard = self._guards.get(project_id)
            if guard is None:
                guard = threading.Lock()
                self._guards[project_id] = guard
            return guard

    def lock(self, project_id: str) -> ProjectLock:
        pdir = self.project_dir(project_id)
        return ProjectLock(
            project_id, paths.lock_file(pdir), stale_after_seconds=self._lock_stale_seconds
        )

    def events(self, project_id: str) -> JsonlLog:
        return JsonlLog(paths.events_file(self.project_dir(project_id)), fsync=False)

    # ------------------------------------------------------------- recovery

    def recover_incomplete_writes(self) -> list[Path]:
        removed = remove_stale_temp_files(paths.projects_root(self._root))
        if removed:
            logger.warning(
                "removed leftover temporary files from interrupted writes",
                extra={"count": len(removed)},
            )
        return removed

    # ---------------------------------------------------------------- reads

    def _load_project(self, pdir: Path) -> Project:
        try:
            raw = load_json(paths.project_file(pdir))
        except FileNotFoundError:
            raise ProjectNotFoundError(pdir.name) from None
        return Project.model_validate(migrate("project", raw))

    def _load_ontology(self, pdir: Path) -> ClassOntology:
        path = paths.classes_file(pdir)
        if not path.exists():
            return ClassOntology()
        return ClassOntology.model_validate(migrate("classes", load_json(path)))

    def get_project(self, project_id: str) -> Project:
        return self._load_project(self.project_dir(project_id))

    def get_ontology(self, project_id: str) -> ClassOntology:
        pdir = self.project_dir(project_id)
        if not paths.project_file(pdir).exists():
            raise ProjectNotFoundError(project_id)
        return self._load_ontology(pdir)

    def get_ui_state(self, project_id: str) -> UiState:
        pdir = self.project_dir(project_id)
        path = paths.ui_state_file(pdir)
        if not path.exists():
            return UiState()
        try:
            return UiState.model_validate(load_json(path))
        except ValueError:
            logger.warning(
                "ui_state.json unreadable, using defaults", extra={"project_id": project_id}
            )
            return UiState()

    def exists(self, project_id: str) -> bool:
        try:
            return paths.project_file(self.project_dir(project_id)).exists()
        except AppError:
            return False

    def list_projects(self) -> list[ProjectSummary]:
        summaries: list[ProjectSummary] = []
        root = paths.projects_root(self._root)
        for entry in sorted(root.iterdir()) if root.exists() else []:
            if not entry.is_dir() or not paths.project_file(entry).exists():
                continue
            try:
                project = self._load_project(entry)
                ontology = self._load_ontology(entry)
            except (ValueError, OSError) as exc:
                logger.error(
                    "skipping unreadable project", extra={"path": str(entry), "error": str(exc)}
                )
                continue
            summaries.append(
                ProjectSummary(
                    project_id=project.project_id,
                    name=project.name,
                    created_at=project.created_at,
                    modified_at=project.modified_at,
                    revision=project.revision,
                    status=project.status,
                    dataset_count=len(project.dataset_order),
                    class_count=len(ontology.classes),
                    channel_count=len(project.channels),
                    first_dataset_id=project.dataset_order[0] if project.dataset_order else None,
                    first_channel_key=project.channels[0].key,
                )
            )
        summaries.sort(key=lambda s: s.created_at, reverse=True)
        return summaries

    # --------------------------------------------------------------- writes

    def create_project(
        self,
        name: str,
        *,
        channels: list[ChannelDefinition] | None = None,
        actor: str | None = None,
    ) -> Project:
        clean_name = sanitize_display_name(name)
        try:
            channel_list = validate_channels(list(channels) if channels else default_channels())
        except ValueError as exc:
            raise AppError("PROJECT_INVALID", str(exc), status_code=422) from exc
        project_id = new_id("proj")
        pdir = paths.project_dir(self._root, project_id)
        while pdir.exists():  # pragma: no cover - astronomically unlikely
            project_id = new_id("proj")
            pdir = paths.project_dir(self._root, project_id)
        now = utc_now_iso()
        project = Project(
            project_id=project_id,
            name=clean_name,
            created_at=now,
            modified_at=now,
            channels=channel_list,
        )
        for sub in (
            paths.datasets_dir(pdir),
            paths.logs_dir(pdir),
            paths.exports_dir(pdir),
            paths.locks_dir(pdir),
        ):
            sub.mkdir(parents=True, exist_ok=True)
        save_model(paths.classes_file(pdir), ClassOntology(), fsync=self._fsync)
        save_model(paths.ui_state_file(pdir), UiState(), fsync=self._fsync)
        # project.json last: its presence marks the project as complete.
        save_model(paths.project_file(pdir), project, fsync=self._fsync)
        self.record_event(
            project_id,
            operation="project.create",
            new_revision=project.revision,
            actor=actor,
            details={"name": clean_name, "channels": [c.key for c in channel_list]},
        )
        return project

    def mutate_project(
        self,
        project_id: str,
        expected_revision: int,
        mutate: Callable[[Project, ClassOntology], None],
        *,
        operation: str,
        actor: str | None = None,
        source: EventSource = "manual",
        class_ids: list[int] | None = None,
        details: dict[str, Any] | None = None,
    ) -> tuple[Project, ClassOntology]:
        """Read-modify-write with optimistic revision check and audit event."""
        pdir = self.project_dir(project_id)
        with self._guard(project_id):
            project = self._load_project(pdir)
            if project.revision != expected_revision:
                raise RevisionConflictError(
                    {"project_id": project_id}, expected_revision, project.revision
                )
            ontology = self._load_ontology(pdir)
            mutate(project, ontology)
            ontology.validate_unique()
            previous = project.revision
            project.revision = previous + 1
            project.modified_at = utc_now_iso()
            save_model(paths.classes_file(pdir), ontology, fsync=self._fsync)
            save_model(paths.project_file(pdir), project, fsync=self._fsync)
        self.record_event(
            project_id,
            operation=operation,
            previous_revision=previous,
            new_revision=project.revision,
            actor=actor,
            source=source,
            class_ids=class_ids or [],
            details=details or {},
        )
        return project, ontology

    def save_ui_state(self, project_id: str, state: UiState) -> UiState:
        pdir = self.project_dir(project_id)
        if not paths.project_file(pdir).exists():
            raise ProjectNotFoundError(project_id)
        save_model(paths.ui_state_file(pdir), state, fsync=False)
        return state

    def delete_project(
        self, project_id: str, expected_revision: int | None, *, actor: str | None = None
    ) -> Path:
        """Move the project directory to ``workspace/trash`` (recoverable)."""
        pdir = self.project_dir(project_id)
        with self._guard(project_id):
            project = self._load_project(pdir)
            if expected_revision is not None and project.revision != expected_revision:
                raise RevisionConflictError(
                    {"project_id": project_id}, expected_revision, project.revision
                )
            self.record_event(
                project_id,
                operation="project.delete",
                previous_revision=project.revision,
                actor=actor,
                details={"name": project.name},
            )
            trash = paths.trash_dir(self._root)
            trash.mkdir(parents=True, exist_ok=True)
            stamp = utc_now_iso().replace(":", "").replace("+", "_")
            destination = trash / f"{project_id}-{stamp}"
            shutil.move(str(pdir), str(destination))
        return destination

    # --------------------------------------------------------------- events

    def record_event(
        self,
        project_id: str,
        *,
        operation: str,
        dataset_id: str | None = None,
        region_ids: list[str] | None = None,
        class_ids: list[int] | None = None,
        previous_revision: int | None = None,
        new_revision: int | None = None,
        source: EventSource = "manual",
        actor: str | None = None,
        provenance: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> Event:
        event = Event(
            project_id=project_id,
            dataset_id=dataset_id,
            operation=operation,
            region_ids=region_ids or [],
            class_ids=class_ids or [],
            previous_revision=previous_revision,
            new_revision=new_revision,
            source=source,
            actor=actor,
            provenance=provenance or {},
            details=details or {},
        )
        self.events(project_id).append(event.model_dump())
        return event

    def list_events(self, project_id: str, limit: int = 200) -> list[Event]:
        records = self.events(project_id).tail(limit)
        return [Event.model_validate(record) for record in records]

    def lock_state(self, project_id: str, owner_id: str | None) -> LockState:
        return self.lock(project_id).status(owner_id)
