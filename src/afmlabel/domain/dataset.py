# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Dataset: N co-registered source channels sharing one annotation grid.

Schema v1 stored exactly two channels as the fields ``topography`` and
``friction``; schema v2 stores an ordered ``channels`` list whose keys
match the project's channel definitions. The two classic channels keep
their keys, so a migrated v1 dataset is a v2 dataset with
``channels=[topography, friction]``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from afmlabel.domain.common import DATASET_SCHEMA_VERSION
from afmlabel.domain.project import ChannelKey

#: Keys of the classic two-channel layout (kept for callers and docs).
MODALITIES: tuple[ChannelKey, ...] = ("topography", "friction")

DatasetStatus = Literal["ready", "blocked"]


class Calibration(BaseModel):
    """Spatial calibration when the source carries it; never invented."""

    pixel_size_x: float | None = None
    pixel_size_y: float | None = None
    unit: str | None = None
    #: Where the values came from, e.g. ``tiff_resolution``, ``image_description``.
    source: str | None = None


class SourceImage(BaseModel):
    #: Project channel key this image fills (``topography``, ``channel_3`` ...).
    channel: ChannelKey
    #: File name inside ``source/`` (always ``<channel><ext>``).
    stored_filename: str
    original_filename: str
    sha256: str
    size_bytes: int
    width: int
    height: int
    dtype: str
    bit_depth: int
    channels: int
    format: str
    lossy_format: bool = False
    calibration: Calibration = Field(default_factory=Calibration)
    imported_at: str
    #: Free-form decoder notes (e.g. TIFF compression, mode).
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceReplacement(BaseModel):
    channel: ChannelKey
    replaced_at: str
    previous_sha256: str
    new_sha256: str
    previous_original_filename: str
    new_original_filename: str
    dataset_revision_before: int


class UnknownChannelError(KeyError):
    def __init__(self, dataset_id: str, channel: str) -> None:
        super().__init__(channel)
        self.dataset_id = dataset_id
        self.channel = channel

    def __str__(self) -> str:
        return f"dataset {self.dataset_id} has no channel {self.channel!r}"


class Dataset(BaseModel):
    schema_version: int = DATASET_SCHEMA_VERSION
    dataset_id: str
    project_id: str
    #: Human-readable name, defaults to the pairing key (e.g. ``239``).
    name: str
    pairing_key: str
    #: One source image per project channel, in project channel order.
    channels: list[SourceImage] = Field(min_length=1)
    width: int
    height: int
    created_at: str
    modified_at: str
    revision: int = 1
    status: DatasetStatus = "ready"
    block_reason: str | None = None
    #: Dataset revision the latest validation report was computed against.
    validated_revision: int | None = None
    import_batch_id: str | None = None
    pairing_method: Literal["key", "order", "manual"] = "key"
    replacements: list[SourceReplacement] = Field(default_factory=list)

    @field_validator("channels")
    @classmethod
    def _unique_channels(cls, value: list[SourceImage]) -> list[SourceImage]:
        keys = [s.channel for s in value]
        if len(set(keys)) != len(keys):
            raise ValueError("dataset channels must have unique keys")
        return value

    @property
    def channel_keys(self) -> list[ChannelKey]:
        return [s.channel for s in self.channels]

    def has_channel(self, channel: ChannelKey) -> bool:
        return any(s.channel == channel for s in self.channels)

    def source(self, channel: ChannelKey) -> SourceImage:
        for image in self.channels:
            if image.channel == channel:
                return image
        raise UnknownChannelError(self.dataset_id, channel)

    def replace_source(self, image: SourceImage) -> None:
        self.channels = [image if s.channel == image.channel else s for s in self.channels]

    # Convenience accessors for the classic layout (tests, docs, exports).
    @property
    def topography(self) -> SourceImage:
        return self.source("topography")

    @property
    def friction(self) -> SourceImage:
        return self.source("friction")


class SourceSummary(BaseModel):
    channel: ChannelKey
    original_filename: str
    sha256: str


class DatasetSummary(BaseModel):
    dataset_id: str
    name: str
    pairing_key: str
    width: int
    height: int
    revision: int
    status: DatasetStatus
    block_reason: str | None
    validated_revision: int | None
    region_count: int
    created_at: str
    modified_at: str
    #: One entry per channel, in project channel order.
    sources: list[SourceSummary] = Field(default_factory=list)

    def source_filename(self, channel: ChannelKey) -> str | None:
        for item in self.sources:
            if item.channel == channel:
                return item.original_filename
        return None

    @property
    def topography_filename(self) -> str:
        return self.sources[0].original_filename if self.sources else ""

    @property
    def friction_filename(self) -> str:
        return self.sources[1].original_filename if len(self.sources) > 1 else ""
