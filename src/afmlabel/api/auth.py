# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Login / logout / status endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from afmlabel.auth.sessions import SESSION_COOKIE_NAME
from afmlabel.errors import AppError, AuthError
from afmlabel.privacy.redaction import register_secret

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class AuthStatus(BaseModel):
    authenticated: bool
    username: str | None = None
    account_configured: bool
    session_expires_at: str | None = None


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_cookie(request: Request, response: Response, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=settings.security.session_timeout_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.security.secure_cookies,
        path="/",
    )


@router.post("/login")
def login(payload: LoginRequest, request: Request, response: Response) -> AuthStatus:
    # Never let the password reach a log line even if something logs the payload.
    register_secret(payload.password)
    accounts = request.app.state.accounts
    sessions = request.app.state.sessions
    limiter = request.app.state.login_limiter
    key = _client_key(request)

    if not accounts.is_configured():
        raise AppError(
            "AUTH_NOT_CONFIGURED",
            "no application account exists yet",
            status_code=503,
            recovery="run `afmlabel auth set-password` on the server",
        )
    if limiter.is_blocked(key):
        retry = limiter.retry_after_seconds(key)
        raise AppError(
            "AUTH_RATE_LIMITED",
            "too many failed login attempts",
            status_code=429,
            recovery=f"wait {retry} seconds and try again",
            details={"retry_after_seconds": retry},
        )
    if not accounts.verify(payload.username, payload.password):
        limiter.record_failure(key)
        logger.warning("login failed", extra={"client": key, "username": payload.username})
        raise AuthError(
            "AUTH_INVALID",
            "invalid username or password",
            recovery="check the credentials configured with `afmlabel auth set-password`",
        )
    limiter.reset(key)
    token, session = sessions.create(payload.username.strip())
    register_secret(token)
    _set_cookie(request, response, token)
    logger.info("login succeeded", extra={"client": key, "username": session.username})
    return AuthStatus(
        authenticated=True,
        username=session.username,
        account_configured=True,
        session_expires_at=session.expires_at,
    )


@router.post("/logout")
def logout(request: Request, response: Response) -> AuthStatus:
    sessions = request.app.state.sessions
    sessions.revoke(request.cookies.get(SESSION_COOKIE_NAME))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return AuthStatus(
        authenticated=False, account_configured=request.app.state.accounts.is_configured()
    )


@router.get("/status")
def status(request: Request) -> AuthStatus:
    session = getattr(request.state, "session", None)
    return AuthStatus(
        authenticated=session is not None,
        username=session.username if session else None,
        account_configured=request.app.state.accounts.is_configured(),
        session_expires_at=session.expires_at if session else None,
    )
