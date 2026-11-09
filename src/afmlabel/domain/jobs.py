# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Local inference job records."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field

from afmlabel.domain.common import new_id, utc_now_iso
from afmlabel.domain.project import ChannelKey


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)


class PointPromptModel(BaseModel):
    x: float
    y: float
    positive: bool = True


class SegmentRequest(BaseModel):
    backend_id: str
    #: Source channel the model sees (``modality`` is the legacy spelling).
    #: ``None`` means the dataset's first channel.
    channel: ChannelKey | None = Field(
        default=None, validation_alias=AliasChoices("channel", "modality")
    )
    points: list[PointPromptModel] = Field(default_factory=list)
    box_xyxy: list[float] | None = None
    #: Existing region whose mask seeds the prompt (mask prompt).
    previous_region_id: str | None = None
    text_prompt: str | None = None


class JobError(BaseModel):
    code: str
    message: str
    recovery: str | None = None


class ProposalOut(BaseModel):
    mask: dict[str, Any]
    score: float | None
    area: int
    bbox_xywh: list[int]
    backend_metadata: dict[str, Any] = Field(default_factory=dict)


class SegmentResult(BaseModel):
    proposals: list[ProposalOut]
    provenance: dict[str, Any]


class JobRecord(BaseModel):
    job_id: str = Field(default_factory=lambda: new_id("job", 12))
    kind: str = "segment"
    project_id: str
    dataset_id: str
    backend_id: str
    status: JobStatus = JobStatus.QUEUED
    created_at: str = Field(default_factory=utc_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    progress: float = 0.0
    message: str | None = None
    error: JobError | None = None
    request: SegmentRequest
    result: SegmentResult | None = None
    actor: str | None = None
    cancel_requested: bool = False

    def summary(self) -> JobRecord:
        """Copy without proposal masks for listings."""
        copy = self.model_copy(deep=False)
        result = self.result
        if result is not None:
            copy.result = SegmentResult(
                proposals=[
                    ProposalOut(
                        mask={},
                        score=p.score,
                        area=p.area,
                        bbox_xywh=p.bbox_xywh,
                        backend_metadata=p.backend_metadata,
                    )
                    for p in result.proposals
                ],
                provenance=result.provenance,
            )
        return copy
