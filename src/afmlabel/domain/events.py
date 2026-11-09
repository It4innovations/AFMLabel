# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Append-only audit event."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from afmlabel.domain.common import new_id, utc_now_iso

EventSource = Literal[
    "manual",
    "system",
    "auth",
    "sam_proposal",
    "tswat_import",
    "lossy_import",
    "boundary_fill",
    "pair_import",
    "export",
]


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt", 12))
    timestamp: str = Field(default_factory=utc_now_iso)
    project_id: str
    dataset_id: str | None = None
    operation: str
    region_ids: list[str] = Field(default_factory=list)
    class_ids: list[int] = Field(default_factory=list)
    previous_revision: int | None = None
    new_revision: int | None = None
    source: EventSource = "manual"
    actor: str | None = None
    #: Backend/import provenance for generated or imported content.
    provenance: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
