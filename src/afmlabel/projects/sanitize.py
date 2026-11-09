# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Display-name sanitisation ("project IDs and sanitized display names")."""

from __future__ import annotations

import re
import unicodedata

from afmlabel.errors import AppError

MAX_NAME_LENGTH = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize_display_name(raw: str, *, what: str = "project name") -> str:
    text = unicodedata.normalize("NFC", raw or "")
    text = _CONTROL.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise AppError("PROJECT_INVALID", f"{what} must not be empty", status_code=422)
    if len(text) > MAX_NAME_LENGTH:
        raise AppError(
            "PROJECT_INVALID",
            f"{what} is too long (max {MAX_NAME_LENGTH} characters)",
            status_code=422,
        )
    return text


def safe_filename(raw: str, *, default: str = "file") -> str:
    """Reduce an untrusted upload filename to a safe basename."""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").split("/")[-1]
    name = _CONTROL.sub("", name)
    name = re.sub(r"[^A-Za-z0-9._ \-()+]", "_", name).strip(" .")
    if not name or name in {".", ".."}:
        return default
    return name[:180]
