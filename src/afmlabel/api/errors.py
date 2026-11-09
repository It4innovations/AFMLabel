# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""JSON error envelope handlers for the HTTP API.

Every API error body has the shape::

    {"error": {"code": "PAIR_DIMENSION_MISMATCH", "message": "...",
               "target": {"project_id": "...", "dataset_id": "..."},
               "recovery": "..."}}
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from afmlabel.errors import (  # noqa: F401 - re-exported for API modules
    ERROR_CODES,
    AppError,
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RevisionConflictError,
)
from afmlabel.privacy.redaction import redact_value

logger = logging.getLogger(__name__)


def error_response(code: str, message: str, status_code: int, **kwargs: Any) -> JSONResponse:
    payload = AppError(code, message, status_code=status_code, **kwargs).to_payload()
    return JSONResponse(status_code=status_code, content=redact_value(payload))


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=redact_value(exc.to_payload()))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "VALIDATION_ERROR"
        if exc.status_code == 404:
            code = "NOT_FOUND"
        elif exc.status_code == 401:
            code = "AUTH_REQUIRED"
        elif exc.status_code == 403:
            code = "CSRF_REJECTED"
        elif exc.status_code >= 500:
            code = "INTERNAL_ERROR"
        return error_response(code, str(exc.detail), exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            "VALIDATION_ERROR",
            "request validation failed",
            422,
            details={"errors": [redact_value(dict(e)) for e in exc.errors()]},
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error", extra={"error_type": type(exc).__name__})
        return error_response(
            "INTERNAL_ERROR",
            "unexpected server error; see the application log",
            500,
            recovery="retry the operation; if it persists, inspect the application log",
        )
