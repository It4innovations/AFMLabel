# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Authentication and same-origin enforcement middleware.

* Every request outside the public allow-list needs a valid session cookie.
  API paths get a ``401 AUTH_REQUIRED`` JSON envelope; page paths are
  redirected to ``/login`` (or ``/setup`` while no account exists).
* State-changing API requests (``POST``/``PUT``/``PATCH``/``DELETE``) must
  carry ``X-Requested-With: AFMLabel`` and, when the browser sends an
  ``Origin`` or ``Sec-Fetch-Site`` header, it must be same-origin. Together
  with ``SameSite=Lax`` cookies this blocks cross-site request forgery
  without a token round-trip.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

from afmlabel.auth.account import AccountStore
from afmlabel.auth.sessions import SESSION_COOKIE_NAME, SessionStore
from afmlabel.errors import AppError

CSRF_HEADER = "x-requested-with"
CSRF_HEADER_VALUE = "AFMLabel"

PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/login",
        "/setup",
        "/api/v1/health",
        "/api/v1/version",
        "/api/v1/auth/login",
        "/api/v1/auth/status",
        "/api/v1/help/registry",
        "/favicon.ico",
    }
)
PUBLIC_PREFIXES: tuple[str, ...] = ("/static/",)
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def _same_origin(request: Request) -> bool:
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if origin:
        return urlsplit(origin).netloc == host
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site:
        return fetch_site in ("same-origin", "none")
    referer = request.headers.get("referer")
    if referer:
        return urlsplit(referer).netloc == host
    return True


def csrf_violation(request: Request) -> str | None:
    if request.method not in UNSAFE_METHODS:
        return None
    if request.headers.get(CSRF_HEADER) != CSRF_HEADER_VALUE:
        return f"missing {CSRF_HEADER} header"
    if not _same_origin(request):
        return "cross-origin request rejected"
    return None


def _error_json(error: AppError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.to_payload())


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, sessions: SessionStore, accounts: AccountStore) -> None:
        super().__init__(app)
        self._sessions = sessions
        self._accounts = accounts

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        path = request.url.path
        is_api = path.startswith("/api/")
        session = self._sessions.resolve(request.cookies.get(SESSION_COOKIE_NAME))
        request.state.session = session

        if is_api:
            violation = csrf_violation(request)
            if violation is not None:
                return _error_json(
                    AppError(
                        "CSRF_REJECTED",
                        violation,
                        status_code=403,
                        recovery="send requests from the AFMLabel UI with the "
                        "X-Requested-With: AFMLabel header",
                    )
                )

        if _is_public(path) or session is not None:
            return await call_next(request)

        if is_api:
            return _error_json(
                AppError(
                    "AUTH_REQUIRED",
                    "authentication required",
                    status_code=401,
                    recovery="log in at /login",
                )
            )
        if not self._accounts.is_configured():
            return RedirectResponse("/setup", status_code=303)
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)
