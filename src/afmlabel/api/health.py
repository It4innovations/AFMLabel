# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Health, version, resource and backend-listing endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from pydantic import BaseModel

from afmlabel import __version__
from afmlabel.diagnostics.system import SystemDiagnosticsReport, run_system_diagnostics
from afmlabel.errors import AppError
from afmlabel.resources.monitor import ResourceSnapshot
from afmlabel.segmentation.base import BackendDescriptor
from afmlabel.segmentation.registry import BackendNotLoadableError, UnknownBackendError

router = APIRouter(prefix="/api/v1", tags=["system"])


class VersionResponse(BaseModel):
    version: str
    api_version: str = "v1"
    uptime_seconds: float


class BackendDescriptorResponse(BaseModel):
    backend_id: str
    display_name: str
    capabilities: list[str]
    available: bool
    unavailable_reason: str | None
    model_repository: str | None
    model_revision: str | None
    checkpoint_sha256: str | None
    code_revision: str | None
    device: str | None
    dtype: str | None
    warnings: list[str]
    #: loaded | not_loaded | loading | failed | unavailable
    load_state: str

    @classmethod
    def from_descriptor(cls, descriptor: BackendDescriptor) -> BackendDescriptorResponse:
        return cls(
            backend_id=descriptor.backend_id,
            display_name=descriptor.display_name,
            capabilities=sorted(capability.value for capability in descriptor.capabilities),
            available=descriptor.available,
            unavailable_reason=descriptor.unavailable_reason,
            model_repository=descriptor.model_repository,
            model_revision=descriptor.model_revision,
            checkpoint_sha256=descriptor.checkpoint_sha256,
            code_revision=descriptor.code_revision,
            device=descriptor.device,
            dtype=descriptor.dtype,
            warnings=list(descriptor.warnings),
            load_state=descriptor.load_state,
        )


class ModelsResponse(BaseModel):
    backends: list[BackendDescriptorResponse]
    default_backend_id: str


@router.get("/health")
def health(request: Request) -> SystemDiagnosticsReport:
    settings = request.app.state.settings
    snapshot = request.app.state.resource_monitor.snapshot()
    return run_system_diagnostics(settings, snapshot)


@router.get("/version")
def version(request: Request) -> VersionResponse:
    started = request.app.state.started_at_monotonic
    return VersionResponse(version=__version__, uptime_seconds=round(time.monotonic() - started, 1))


@router.get("/system/resources")
def system_resources(request: Request) -> ResourceSnapshot:
    snapshot: ResourceSnapshot = request.app.state.resource_monitor.snapshot()
    return snapshot


@router.get("/models")
def models(request: Request) -> ModelsResponse:
    registry = request.app.state.registry
    settings = request.app.state.settings
    return ModelsResponse(
        backends=[
            BackendDescriptorResponse.from_descriptor(descriptor)
            for descriptor in registry.list_descriptors()
        ],
        default_backend_id=settings.runtime.default_backend,
    )


@router.post("/models/{backend_id}/load", status_code=202)
def load_model(backend_id: str, request: Request) -> BackendDescriptorResponse:
    """Start loading a configured model backend in the background.

    Returns the descriptor immediately (``load_state`` ``loading`` or
    ``loaded``); poll ``GET /models`` until it is ``loaded`` or ``failed``.
    """
    registry = request.app.state.registry
    try:
        descriptor = registry.start_load(backend_id)
    except UnknownBackendError as exc:
        raise AppError(
            "MODEL_UNAVAILABLE",
            str(exc),
            status_code=404,
            target={"backend_id": backend_id},
            recovery="pick a backend listed by /api/v1/models",
        ) from exc
    except BackendNotLoadableError as exc:
        raise AppError(
            "MODEL_UNAVAILABLE",
            f"backend {backend_id} cannot be loaded: {exc.reason}",
            status_code=409,
            target={"backend_id": backend_id},
            recovery="fix the configuration or model files (README.md, optional SAM backends)",
        ) from exc
    return BackendDescriptorResponse.from_descriptor(descriptor)
