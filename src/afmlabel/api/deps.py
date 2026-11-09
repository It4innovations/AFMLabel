# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Request-scoped dependencies shared by API routers."""

from __future__ import annotations

from fastapi import Request

from afmlabel.auth.sessions import Session
from afmlabel.projects.store import ProjectStore
from afmlabel.settings import AppSettings

EDITOR_HEADER = "x-afmlabel-editor"


def get_settings(request: Request) -> AppSettings:
    settings: AppSettings = request.app.state.settings
    return settings


def get_store(request: Request) -> ProjectStore:
    store: ProjectStore = request.app.state.project_store
    return store


def get_session(request: Request) -> Session | None:
    session = getattr(request.state, "session", None)
    return session if isinstance(session, Session) else None


def get_actor(request: Request) -> str | None:
    session = get_session(request)
    if session is None:
        return None
    return f"{session.username}@{session.session_id}"


def get_editor_id(request: Request) -> str:
    """Identity of the editing browser tab for project locks.

    The UI generates a per-tab id and sends it as ``X-AFMLabel-Editor``; a
    caller without the header (scripts, tests) falls back to its session.
    """
    header = request.headers.get(EDITOR_HEADER)
    if header and len(header) <= 64:
        return header
    session = get_session(request)
    return session.session_id if session else "anonymous"


def get_editor_label(request: Request) -> str:
    session = get_session(request)
    agent = request.headers.get("user-agent", "")[:40]
    user = session.username if session else "anonymous"
    return f"{user} ({agent})" if agent else user
