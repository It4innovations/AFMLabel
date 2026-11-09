# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Shared helpers for domain models: timestamps, identifiers, schema versions."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

PROJECT_SCHEMA_VERSION = 2
CLASSES_SCHEMA_VERSION = 1
DATASET_SCHEMA_VERSION = 2
REGIONS_SCHEMA_VERSION = 1
VALIDATION_SCHEMA_VERSION = 1

_ID_PATTERN = re.compile(r"^[a-z]+_[0-9a-f]{6,32}$")


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def new_id(prefix: str, length: int = 10) -> str:
    """Random, URL-safe, filesystem-safe identifier such as ``proj_3fa9c1b2d4``."""
    return f"{prefix}_{secrets.token_hex(length // 2 + 1)[:length]}"


def is_valid_id(value: str) -> bool:
    return bool(_ID_PATTERN.match(value))
