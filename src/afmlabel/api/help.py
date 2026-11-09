# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Local Help pages.

``/help`` lists the user documentation, ``/help/<name>`` renders one
Markdown file from the repository ``docs`` directory (or the packaged copy)
with a minimal local converter, and ``/api/v1/help/registry`` serves the
contextual-help registry. Nothing here contacts the network.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from afmlabel.help.markdown import render_markdown
from afmlabel.help.registry import load_registry, load_shortcuts
from afmlabel.templating import templates

router = APIRouter(tags=["help"])

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,80}$")

#: Order and titles of the Help index; only these files are served.
HELP_INDEX: list[tuple[str, str, str]] = [
    (
        "user-guide",
        "User guide",
        "The complete workflow: projects, channels, import, annotation, fill tool, "
        "SAM, Findings, validation, export, administration and keyboard shortcuts.",
    ),
]


def docs_directories() -> list[Path]:
    package_docs = Path(__file__).resolve().parent.parent / "docs"
    repo_docs = Path(__file__).resolve().parents[3] / "docs"
    return [d for d in (package_docs, repo_docs) if d.is_dir()]


def find_doc(name: str) -> Path | None:
    if not _NAME.match(name):
        return None
    for directory in docs_directories():
        candidate = directory / f"{name}.md"
        if candidate.is_file():
            return candidate
    return None


def _session(request: Request) -> Any:
    return getattr(request.state, "session", None)


@router.get("/help", response_class=HTMLResponse)
def help_index(request: Request) -> HTMLResponse:
    available = [
        {"name": name, "title": title, "summary": summary, "available": find_doc(name) is not None}
        for name, title, summary in HELP_INDEX
    ]
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "session": _session(request),
            "index": available,
            "doc": None,
            "shortcuts": load_shortcuts(),
        },
    )


@router.get("/help/{name}", response_class=HTMLResponse)
def help_page(name: str, request: Request) -> HTMLResponse:
    path = find_doc(name)
    if path is None:
        return templates.TemplateResponse(
            request,
            "help.html",
            {
                "session": _session(request),
                "index": [
                    {"name": n, "title": t, "summary": s, "available": find_doc(n) is not None}
                    for n, t, s in HELP_INDEX
                ],
                "doc": {
                    "name": name,
                    "title": "Not found",
                    "html": "",
                    "headings": [],
                    "missing": True,
                },
                "shortcuts": load_shortcuts(),
            },
            status_code=404,
        )
    rendered = render_markdown(path.read_text(encoding="utf-8"))
    title = rendered.title or dict((n, t) for n, t, _ in HELP_INDEX).get(name, name)
    return templates.TemplateResponse(
        request,
        "help.html",
        {
            "session": _session(request),
            "index": [
                {"name": n, "title": t, "summary": s, "available": find_doc(n) is not None}
                for n, t, s in HELP_INDEX
            ],
            "doc": {
                "name": name,
                "title": title,
                "html": rendered.html,
                "headings": [h for h in rendered.headings if h.level <= 3],
                "missing": False,
            },
            "shortcuts": load_shortcuts(),
        },
    )


@router.get("/api/v1/help/registry")
def help_registry() -> JSONResponse:
    return JSONResponse({"entries": load_registry(), "shortcuts": load_shortcuts()})
