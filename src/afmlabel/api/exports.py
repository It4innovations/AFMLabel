# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Export endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from afmlabel.api.deps import get_actor
from afmlabel.api.projects import require_editing_rights
from afmlabel.exports.base import ExportOptions
from afmlabel.exports.service import PROFILES, ExportRecord, ExportService

router = APIRouter(prefix="/api/v1/projects/{project_id}/exports", tags=["exports"])


def get_exports(request: Request) -> ExportService:
    service: ExportService = request.app.state.export_service
    return service


class ExportRequest(BaseModel):
    #: ``None`` selects the configured default profile (NanoUNet).
    profile_id: str | None = None
    dataset_ids: list[str] | None = None
    options: ExportOptions = Field(default_factory=ExportOptions)


class ProfileInfo(BaseModel):
    profile_id: str
    display_name: str
    is_default: bool = False


def default_profile_id(request: Request) -> str:
    configured: str = request.app.state.settings.export.default_profile
    return configured if configured in PROFILES else "nanounet"


@router.get("/profiles")
def list_profiles(request: Request) -> list[ProfileInfo]:
    default = default_profile_id(request)
    profiles = [
        ProfileInfo(
            profile_id=p.profile_id, display_name=p.display_name, is_default=p.profile_id == default
        )
        for p in PROFILES.values()
    ]
    profiles.sort(key=lambda p: not p.is_default)
    return profiles


@router.post("", status_code=201)
def create_export(
    project_id: str,
    payload: ExportRequest,
    request: Request,
    service: ExportService = Depends(get_exports),
) -> ExportRecord:
    require_editing_rights(request, project_id)
    return service.run(
        project_id,
        profile_id=payload.profile_id or default_profile_id(request),
        dataset_ids=payload.dataset_ids,
        options=payload.options,
        actor=get_actor(request),
    )


@router.get("")
def list_exports(
    project_id: str, service: ExportService = Depends(get_exports)
) -> list[ExportRecord]:
    return service.list_exports(project_id)


@router.get("/{export_id}")
def get_export(
    project_id: str, export_id: str, service: ExportService = Depends(get_exports)
) -> ExportRecord:
    return service.get(project_id, export_id)


@router.get("/{export_id}/download")
def download_export(
    project_id: str, export_id: str, service: ExportService = Depends(get_exports)
) -> FileResponse:
    path = service.package_path(project_id, export_id)
    return FileResponse(path, media_type="application/zip", filename=path.name)


@router.delete("/{export_id}", status_code=204)
def delete_export(
    project_id: str, export_id: str, request: Request, service: ExportService = Depends(get_exports)
) -> Response:
    require_editing_rights(request, project_id)
    service.delete(project_id, export_id, actor=get_actor(request))
    return Response(status_code=204)
