# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Project, class-ontology, UI-state, lock and event endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from afmlabel.api.deps import (
    get_actor,
    get_editor_id,
    get_editor_label,
    get_store,
)
from afmlabel.domain.events import Event
from afmlabel.domain.project import (
    DEFAULT_CHANNEL_COUNT,
    MAX_CHANNELS,
    MIN_CHANNELS,
    ChannelDefinition,
    ClassDefinition,
    ClassOntology,
    LockInfo,
    LockState,
    Project,
    ProjectConfig,
    ProjectSummary,
    SemanticRole,
    UiState,
    default_channels,
    normalize_class_name,
    stable_key_from_name,
    validate_channels,
)
from afmlabel.errors import AppError, NotFoundError
from afmlabel.projects.sanitize import sanitize_display_name
from afmlabel.projects.store import ProjectStore
from afmlabel.settings import BoundaryMode

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

_DEFAULT_COLORS = [
    "#00a398",
    "#fa3253",
    "#3d3df5",
    "#faad14",
    "#b83df5",
    "#52c41a",
    "#fa32b7",
    "#33ddff",
    "#ff6037",
    "#83e070",
    "#32b7fa",
    "#fafa37",
]


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=400)
    #: Number of co-registered source images per dataset (1..64, default 2).
    channel_count: int = Field(default=DEFAULT_CHANNEL_COUNT, ge=MIN_CHANNELS, le=MAX_CHANNELS)
    #: Explicit channel definitions; when given they override ``channel_count``.
    channels: list[ChannelDefinition] | None = None

    def resolved_channels(self) -> list[ChannelDefinition]:
        if self.channels:
            return list(self.channels)
        return default_channels(self.channel_count)


class ProjectPatchRequest(BaseModel):
    expected_revision: int
    name: str | None = None
    status: str | None = None
    config: ProjectConfig | None = None
    #: Channel definitions may change only while the project has no datasets.
    channels: list[ChannelDefinition] | None = None


class ProjectDetail(BaseModel):
    project: Project
    classes: ClassOntology
    ui_state: UiState
    lock: LockState
    effective_boundary_mode: BoundaryMode
    effective_require_overlap_review: bool


class ClassCreateRequest(BaseModel):
    expected_revision: int
    name: str
    display_color: str | None = None
    semantic_role: SemanticRole = SemanticRole.MATERIAL_LAYER
    export_id: int | None = None
    description: str = ""


class ClassPatchRequest(BaseModel):
    expected_revision: int
    name: str | None = None
    display_color: str | None = None
    semantic_role: SemanticRole | None = None
    enabled: bool | None = None
    export_id: int | None = None
    clear_export_id: bool = False
    description: str | None = None


class ClassMutationResponse(BaseModel):
    project: Project
    classes: ClassOntology


class LockRequest(BaseModel):
    force: bool = False


def effective_boundary_mode(request: Request, project: Project) -> BoundaryMode:
    mode: BoundaryMode = (
        project.config.boundary_mode or request.app.state.settings.annotation.boundary_mode
    )
    return mode


def effective_overlap_review(request: Request, project: Project) -> bool:
    if project.config.require_overlap_review is not None:
        return project.config.require_overlap_review
    value: bool = request.app.state.settings.annotation.require_overlap_review
    return value


def require_editing_rights(request: Request, project_id: str) -> None:
    store = get_store(request)
    store.lock(project_id).ensure_held(get_editor_id(request))


# ------------------------------------------------------------------ projects


@router.post("", status_code=201)
def create_project(
    payload: ProjectCreateRequest, request: Request, store: ProjectStore = Depends(get_store)
) -> Project:
    return store.create_project(
        payload.name, channels=payload.resolved_channels(), actor=get_actor(request)
    )


@router.get("")
def list_projects(store: ProjectStore = Depends(get_store)) -> list[ProjectSummary]:
    return store.list_projects()


@router.get("/{project_id}")
def get_project(
    project_id: str, request: Request, store: ProjectStore = Depends(get_store)
) -> ProjectDetail:
    project = store.get_project(project_id)
    return ProjectDetail(
        project=project,
        classes=store.get_ontology(project_id),
        ui_state=store.get_ui_state(project_id),
        lock=store.lock_state(project_id, get_editor_id(request)),
        effective_boundary_mode=effective_boundary_mode(request, project),
        effective_require_overlap_review=effective_overlap_review(request, project),
    )


@router.patch("/{project_id}")
def patch_project(
    project_id: str,
    payload: ProjectPatchRequest,
    request: Request,
    store: ProjectStore = Depends(get_store),
) -> Project:
    require_editing_rights(request, project_id)
    changes: dict[str, Any] = {}

    def mutate(project: Project, _: ClassOntology) -> None:
        if payload.name is not None:
            project.name = sanitize_display_name(payload.name)
            changes["name"] = project.name
        if payload.status is not None:
            if payload.status not in ("active", "archived"):
                raise AppError(
                    "PROJECT_INVALID", "status must be active or archived", status_code=422
                )
            project.status = payload.status  # type: ignore[assignment]
            changes["status"] = payload.status
        if payload.config is not None:
            project.config = payload.config
            changes["config"] = payload.config.model_dump()
        if payload.channels is not None:
            if project.dataset_order:
                raise AppError(
                    "PROJECT_INVALID",
                    "channels cannot change once the project has datasets",
                    status_code=409,
                    target={"project_id": project_id},
                    recovery="delete every dataset first, or create a new project",
                )
            try:
                project.channels = validate_channels(list(payload.channels))
            except ValueError as exc:
                raise AppError("PROJECT_INVALID", str(exc), status_code=422) from exc
            changes["channels"] = [c.model_dump() for c in project.channels]

    project, _ = store.mutate_project(
        project_id,
        payload.expected_revision,
        mutate,
        operation="project.update",
        actor=get_actor(request),
        details=changes,
    )
    return project


@router.delete("/{project_id}", status_code=204)
def delete_project(
    project_id: str,
    request: Request,
    expected_revision: int | None = Query(default=None),
    store: ProjectStore = Depends(get_store),
) -> Response:
    require_editing_rights(request, project_id)
    store.delete_project(project_id, expected_revision, actor=get_actor(request))
    return Response(status_code=204)


# ------------------------------------------------------------------ ui state


@router.get("/{project_id}/ui-state")
def get_ui_state(project_id: str, store: ProjectStore = Depends(get_store)) -> UiState:
    return store.get_ui_state(project_id)


@router.put("/{project_id}/ui-state")
def put_ui_state(
    project_id: str, payload: UiState, store: ProjectStore = Depends(get_store)
) -> UiState:
    return store.save_ui_state(project_id, payload)


# ------------------------------------------------------------------- classes


@router.get("/{project_id}/classes")
def list_classes(project_id: str, store: ProjectStore = Depends(get_store)) -> ClassOntology:
    return store.get_ontology(project_id)


def _unique_stable_key(ontology: ClassOntology, base: str) -> str:
    keys = {c.stable_key for c in ontology.classes}
    if base not in keys:
        return base
    index = 2
    while f"{base}_{index}" in keys:
        index += 1
    return f"{base}_{index}"


@router.post("/{project_id}/classes", status_code=201)
def create_class(
    project_id: str,
    payload: ClassCreateRequest,
    request: Request,
    store: ProjectStore = Depends(get_store),
) -> ClassMutationResponse:
    require_editing_rights(request, project_id)
    created: dict[str, int] = {}

    def mutate(project: Project, ontology: ClassOntology) -> None:
        name = sanitize_display_name(payload.name, what="class name")
        if any(
            normalize_class_name(c.name) == normalize_class_name(name) for c in ontology.classes
        ):
            raise AppError(
                "CLASS_REFERENCE_INVALID",
                f"a class named {name!r} already exists",
                status_code=409,
                target={"project_id": project_id},
                recovery="choose a different class name",
            )
        if payload.semantic_role == SemanticRole.BOUNDARY and ontology.boundary_class() is not None:
            raise AppError(
                "BOUNDARY_POLICY_VIOLATION",
                "only one enabled Boundary class is allowed per project",
                status_code=409,
                target={"project_id": project_id},
                recovery="reuse the existing Boundary class or disable it first",
            )
        class_id = ontology.next_class_id
        color = payload.display_color or _DEFAULT_COLORS[(class_id - 1) % len(_DEFAULT_COLORS)]
        definition = ClassDefinition(
            class_id=class_id,
            stable_key=_unique_stable_key(ontology, stable_key_from_name(name)),
            name=name,
            display_color=color,
            semantic_role=payload.semantic_role,
            export_id=payload.export_id,
            description=payload.description,
        )
        ontology.classes.append(definition)
        ontology.next_class_id = class_id + 1
        created["class_id"] = class_id

    project, ontology = store.mutate_project(
        project_id,
        payload.expected_revision,
        mutate,
        operation="class.create",
        actor=get_actor(request),
        class_ids=[created.get("class_id", 0)],
        details={"name": payload.name},
    )
    return ClassMutationResponse(project=project, classes=ontology)


@router.patch("/{project_id}/classes/{class_id}")
def patch_class(
    project_id: str,
    class_id: int,
    payload: ClassPatchRequest,
    request: Request,
    store: ProjectStore = Depends(get_store),
) -> ClassMutationResponse:
    require_editing_rights(request, project_id)

    def mutate(project: Project, ontology: ClassOntology) -> None:
        definition = ontology.by_id(class_id)
        if definition is None:
            raise NotFoundError(
                "CLASS_REFERENCE_INVALID",
                f"class {class_id} does not exist",
                target={"project_id": project_id, "class_id": class_id},
            )
        update: dict[str, Any] = {}
        if payload.name is not None:
            name = sanitize_display_name(payload.name, what="class name")
            for other in ontology.classes:
                if other.class_id != class_id and normalize_class_name(
                    other.name
                ) == normalize_class_name(name):
                    raise AppError(
                        "CLASS_REFERENCE_INVALID",
                        f"a class named {name!r} already exists",
                        status_code=409,
                        target={"project_id": project_id},
                    )
            update["name"] = name
        if payload.display_color is not None:
            update["display_color"] = payload.display_color
        if payload.semantic_role is not None:
            if payload.semantic_role == SemanticRole.BOUNDARY:
                boundary = ontology.boundary_class()
                if boundary is not None and boundary.class_id != class_id:
                    raise AppError(
                        "BOUNDARY_POLICY_VIOLATION",
                        "only one enabled Boundary class is allowed per project",
                        status_code=409,
                        target={"project_id": project_id},
                    )
            update["semantic_role"] = payload.semantic_role
        if payload.enabled is not None:
            update["enabled"] = payload.enabled
        if payload.clear_export_id:
            update["export_id"] = None
        elif payload.export_id is not None:
            update["export_id"] = payload.export_id
        if payload.description is not None:
            update["description"] = payload.description
        replacement = definition.model_copy(update=update)
        ontology.classes = [replacement if c.class_id == class_id else c for c in ontology.classes]

    project, ontology = store.mutate_project(
        project_id,
        payload.expected_revision,
        mutate,
        operation="class.update",
        actor=get_actor(request),
        class_ids=[class_id],
        details=payload.model_dump(exclude={"expected_revision"}, exclude_none=True),
    )
    return ClassMutationResponse(project=project, classes=ontology)


@router.delete("/{project_id}/classes/{class_id}")
def delete_class(
    project_id: str,
    class_id: int,
    request: Request,
    expected_revision: int = Query(),
    store: ProjectStore = Depends(get_store),
) -> ClassMutationResponse:
    require_editing_rights(request, project_id)
    usage_counter = getattr(request.app.state, "class_usage_counter", None)

    def mutate(project: Project, ontology: ClassOntology) -> None:
        if ontology.by_id(class_id) is None:
            raise NotFoundError(
                "CLASS_REFERENCE_INVALID",
                f"class {class_id} does not exist",
                target={"project_id": project_id, "class_id": class_id},
            )
        in_use = usage_counter(project_id, class_id) if usage_counter else 0
        if in_use:
            raise AppError(
                "CLASS_IN_USE",
                f"class {class_id} is used by {in_use} region(s) and cannot be deleted",
                status_code=409,
                target={"project_id": project_id, "class_id": class_id},
                recovery="reassign or delete those regions first, or disable the class instead",
                details={"region_count": in_use},
            )
        ontology.classes = [c for c in ontology.classes if c.class_id != class_id]

    project, ontology = store.mutate_project(
        project_id,
        expected_revision,
        mutate,
        operation="class.delete",
        actor=get_actor(request),
        class_ids=[class_id],
    )
    return ClassMutationResponse(project=project, classes=ontology)


# --------------------------------------------------------------------- locks


@router.get("/{project_id}/lock")
def lock_status(
    project_id: str, request: Request, store: ProjectStore = Depends(get_store)
) -> LockState:
    store.get_project(project_id)
    return store.lock_state(project_id, get_editor_id(request))


@router.post("/{project_id}/lock")
def acquire_lock(
    project_id: str,
    request: Request,
    payload: LockRequest | None = None,
    store: ProjectStore = Depends(get_store),
) -> LockInfo:
    store.get_project(project_id)
    lock = store.lock(project_id)
    editor = get_editor_id(request)
    before = lock.read()
    info = lock.acquire(editor, get_editor_label(request), force=bool(payload and payload.force))
    if before is not None and before.owner_id != editor:
        store.record_event(
            project_id,
            operation="lock.takeover",
            source="system",
            actor=get_actor(request),
            details={"previous_owner": before.owner_label, "stale": lock.is_stale(before)},
        )
    return info


@router.post("/{project_id}/lock/heartbeat")
def heartbeat_lock(
    project_id: str, request: Request, store: ProjectStore = Depends(get_store)
) -> LockInfo:
    return store.lock(project_id).heartbeat(get_editor_id(request))


@router.delete("/{project_id}/lock", status_code=204)
def release_lock(
    project_id: str, request: Request, store: ProjectStore = Depends(get_store)
) -> Response:
    store.lock(project_id).release(get_editor_id(request))
    return Response(status_code=204)


# -------------------------------------------------------------------- events


@router.get("/{project_id}/events")
def list_events(
    project_id: str,
    limit: int = Query(default=200, ge=1, le=5000),
    store: ProjectStore = Depends(get_store),
) -> list[Event]:
    store.get_project(project_id)
    return store.list_events(project_id, limit)
