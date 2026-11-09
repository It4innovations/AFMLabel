# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Stable machine-readable error codes and the base error type.

Kept free of any HTTP framework import so domain and persistence code can
raise :class:`AppError` without depending on the API layer.
"""

from __future__ import annotations

from typing import Any

ERROR_CODES: frozenset[str] = frozenset(
    {
        "PAIR_MISSING_IMAGE",
        "PAIR_DIMENSION_MISMATCH",
        "PAIR_REGISTRATION_MISMATCH",
        "PAIR_AMBIGUOUS",
        "SOURCE_REPLACEMENT_INCOMPATIBLE",
        "CLASS_REFERENCE_INVALID",
        "CLASS_IN_USE",
        "REGION_REFERENCE_INVALID",
        "REGION_LOCKED",
        "REGION_HIDDEN",
        "OVERLAP_UNREVIEWED",
        "OVERLAP_MULTIPLE",
        "UNLABELED_PIXELS",
        "BOUNDARY_POLICY_VIOLATION",
        "VALIDATION_STALE",
        "VALIDATION_MISSING",
        "VALIDATION_FAILED",
        "TSWAT_IMPORT_INVALID",
        "TSWAT_IMPORT_LOSSY",
        "MODEL_UNAVAILABLE",
        "MODEL_CAPABILITY_UNSUPPORTED",
        "MODEL_CHECKPOINT_INVALID",
        "MODEL_OOM",
        "HF_TOKEN_MISSING",
        "DISK_SPACE_LOW",
        "RESOURCE_METRIC_UNAVAILABLE",
        "AUTH_INVALID",
        "AUTH_REQUIRED",
        "AUTH_RATE_LIMITED",
        "AUTH_NOT_CONFIGURED",
        "CSRF_REJECTED",
        "REVISION_CONFLICT",
        "PROJECT_LOCKED",
        "PROJECT_NOT_FOUND",
        "PROJECT_INVALID",
        "DATASET_NOT_FOUND",
        "CHANNEL_NOT_FOUND",
        "EXPORT_NOT_FOUND",
        "EXPORT_PROFILE_INVALID",
        "NANOUNET_CONTRACT_UNAVAILABLE",
        "UPLOAD_INVALID",
        "UPLOAD_TOO_LARGE",
        "IMAGE_DECODE_FAILED",
        "JOB_NOT_FOUND",
        "JOB_QUEUE_FULL",
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "INTERNAL_ERROR",
    }
)


class AppError(Exception):
    """Domain/application error carrying a stable code and HTTP status."""

    status_code = 400

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        target: dict[str, Any] | None = None,
        recovery: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown error code {code!r}")
        self.code = code
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.target = target or {}
        self.recovery = recovery
        self.details = details or {}
        super().__init__(f"{code}: {message}")

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "target": self.target,
        }
        if self.recovery:
            payload["recovery"] = self.recovery
        if self.details:
            payload["details"] = self.details
        return {"error": payload}


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


class AuthError(AppError):
    status_code = 401


class ForbiddenError(AppError):
    status_code = 403


class RevisionConflictError(ConflictError):
    def __init__(self, target: dict[str, Any], expected: int, actual: int) -> None:
        super().__init__(
            "REVISION_CONFLICT",
            f"expected revision {expected} but the current revision is {actual}; "
            "the state was modified by another session or tab",
            target=target,
            recovery="reload the latest state and reapply the change",
            details={"expected_revision": expected, "current_revision": actual},
        )
