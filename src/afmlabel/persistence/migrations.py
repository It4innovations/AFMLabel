# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Schema versioning and migration interface.

Every persisted document carries ``schema_version``. ``migrate`` walks the
registered step functions from the stored version up to the current one and
returns the migrated raw dictionary; loaders validate the result with the
current Pydantic model. Adding a schema change means: bump the constant in
``domain.common``, register a ``(kind, from_version) -> function`` step here,
and add a fixture test.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from afmlabel.domain.common import (
    CLASSES_SCHEMA_VERSION,
    DATASET_SCHEMA_VERSION,
    PROJECT_SCHEMA_VERSION,
    REGIONS_SCHEMA_VERSION,
    VALIDATION_SCHEMA_VERSION,
)

MigrationStep = Callable[[dict[str, Any]], dict[str, Any]]

CURRENT_VERSIONS: dict[str, int] = {
    "project": PROJECT_SCHEMA_VERSION,
    "classes": CLASSES_SCHEMA_VERSION,
    "dataset": DATASET_SCHEMA_VERSION,
    "regions": REGIONS_SCHEMA_VERSION,
    "validation": VALIDATION_SCHEMA_VERSION,
}

_STEPS: dict[tuple[str, int], MigrationStep] = {}


class MigrationError(ValueError):
    pass


def register_step(kind: str, from_version: int) -> Callable[[MigrationStep], MigrationStep]:
    def decorator(func: MigrationStep) -> MigrationStep:
        _STEPS[(kind, from_version)] = func
        return func

    return decorator


@register_step("project", 1)
def _project_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """v1 projects always had the two classic channels; make them explicit."""
    if not raw.get("channels"):
        raw["channels"] = [
            {
                "key": "topography",
                "label": "Topography",
                "filename_tokens": ["topography", "topo", "height", "z", "hgt"],
            },
            {
                "key": "friction",
                "label": "Friction",
                "filename_tokens": ["friction", "fric", "lateral", "lat", "lfm", "fr"],
            },
        ]
    return raw


@register_step("dataset", 1)
def _dataset_v1_to_v2(raw: dict[str, Any]) -> dict[str, Any]:
    """``topography`` / ``friction`` fields become the ordered ``channels`` list."""
    if "channels" not in raw:
        channels: list[dict[str, Any]] = []
        for key in ("topography", "friction"):
            image = raw.pop(key, None)
            if isinstance(image, dict):
                image = dict(image)
                image.pop("modality", None)
                image["channel"] = key
                channels.append(image)
        raw["channels"] = channels
    for replacement in raw.get("replacements", []) or []:
        if isinstance(replacement, dict) and "channel" not in replacement:
            replacement["channel"] = replacement.pop("modality", "topography")
    return raw


def migrate(kind: str, raw: dict[str, Any]) -> dict[str, Any]:
    current = CURRENT_VERSIONS[kind]
    version = raw.get("schema_version")
    if not isinstance(version, int):
        raise MigrationError(f"{kind}: missing or invalid schema_version")
    if version > current:
        raise MigrationError(
            f"{kind}: schema_version {version} is newer than this build supports ({current}); "
            "upgrade AFMLabel"
        )
    while version < current:
        step = _STEPS.get((kind, version))
        if step is None:
            raise MigrationError(f"{kind}: no migration registered from schema_version {version}")
        raw = step(dict(raw))
        raw["schema_version"] = version + 1
        version += 1
    return raw
