# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Server-rendered HTML pages (Jinja2 templates, no SPA)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from afmlabel import __version__
from afmlabel.api.projects import effective_boundary_mode, effective_overlap_review
from afmlabel.templating import templates

router = APIRouter(tags=["pages"])

templates.env.globals["app_version"] = __version__
_TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def _session(request: Request):  # type: ignore[no-untyped-def]
    return getattr(request.state, "session", None)


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    store = request.app.state.project_store
    return templates.TemplateResponse(
        request,
        "index.html",
        {"projects": store.list_projects(), "session": _session(request)},
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> RedirectResponse:
    return RedirectResponse("/static/img/favicon.svg", status_code=302)


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(request: Request, next: str = "/") -> Response:  # noqa: A002
    if _session(request) is not None:
        return RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    if not request.app.state.accounts.is_configured():
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"next": next if next.startswith("/") else "/", "session": None}
    )


@router.get("/setup", response_class=HTMLResponse, response_model=None)
def setup_page(request: Request) -> Response:
    if request.app.state.accounts.is_configured():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "setup_required.html", {"session": None})


@router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_page(project_id: str, request: Request) -> HTMLResponse:
    store = request.app.state.project_store
    project = store.get_project(project_id)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": project,
            "classes": store.get_ontology(project_id),
            "session": _session(request),
            "effective_boundary_mode": effective_boundary_mode(request, project),
            "effective_require_overlap_review": effective_overlap_review(request, project),
        },
    )


@router.get("/projects/{project_id}/datasets/{dataset_id}", response_class=HTMLResponse)
def workspace_page(project_id: str, dataset_id: str, request: Request) -> HTMLResponse:
    store = request.app.state.project_store
    datasets = request.app.state.dataset_store
    project = store.get_project(project_id)
    dataset = datasets.get(project_id, dataset_id)
    previous, following = datasets.neighbours(project_id, dataset_id)
    order = project.dataset_order
    # Channels in project order that this dataset actually carries.
    channels = [c for c in project.channels if dataset.has_channel(c.key)]
    return templates.TemplateResponse(
        request,
        "workspace.html",
        {
            "project": project,
            "dataset": dataset,
            "channels": channels,
            "channel_data": [{"key": c.key, "label": c.label} for c in channels],
            "classes": store.get_ontology(project_id),
            "ui_state": store.get_ui_state(project_id),
            "session": _session(request),
            "previous_dataset_id": previous,
            "next_dataset_id": following,
            "dataset_index": order.index(dataset_id) + 1 if dataset_id in order else 0,
            "dataset_total": len(order),
            "effective_boundary_mode": effective_boundary_mode(request, project),
            "effective_require_overlap_review": effective_overlap_review(request, project),
        },
    )
