# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Project, class ontology and UI resume state."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator

from afmlabel.domain.common import CLASSES_SCHEMA_VERSION, PROJECT_SCHEMA_VERSION
from afmlabel.settings import BoundaryMode

_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
_STABLE_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TOKEN_PATTERN = re.compile(r"^[a-z0-9]{1,32}$")

#: Key of one source channel of a dataset (``topography``, ``friction``,
#: ``channel_3`` ...). Historically the only two channels were called
#: *modalities*; the alias is kept for readers of older code.
ChannelKey = str
Modality = ChannelKey

MIN_CHANNELS = 1
MAX_CHANNELS = 64
DEFAULT_CHANNEL_COUNT = 2

#: Filename words that identify the two classic AFM channels.
_CLASSIC_TOKENS: dict[str, tuple[str, ...]] = {
    "topography": ("topography", "topo", "height", "z", "hgt"),
    "friction": ("friction", "fric", "lateral", "lat", "lfm", "fr"),
}


class ChannelDefinition(BaseModel):
    """One source image slot every dataset of the project must fill.

    ``key`` is the stable machine identifier (stored filename stem, API path
    segment, export folder); ``label`` is what the UI shows; and
    ``filename_tokens`` are the words removed from an uploaded filename to
    obtain the pairing key (``239_topography.tiff`` -> ``239``).
    """

    key: str
    label: str
    filename_tokens: list[str] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        if not _STABLE_KEY_PATTERN.match(value):
            raise ValueError("channel key must match [a-z][a-z0-9_]*")
        return value

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        value = re.sub(r"\s+", " ", value.strip())
        if not value:
            raise ValueError("channel label must not be empty")
        if len(value) > 60:
            raise ValueError("channel label is too long (max 60 characters)")
        return value

    @field_validator("filename_tokens")
    @classmethod
    def _validate_tokens(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for token in value:
            token = token.strip().casefold()
            if not token:
                continue
            if not _TOKEN_PATTERN.match(token):
                raise ValueError(f"filename token {token!r} must be alphanumeric (max 32 chars)")
            if token not in cleaned:
                cleaned.append(token)
        return cleaned


def default_channel(index: int) -> ChannelDefinition:
    """Default definition of the ``index``-th (1-based) channel.

    The first two keep the historical topography / friction identity so
    existing projects, filename conventions and the NanoUNet export
    layout are unchanged; further channels are generic and renameable.
    """
    if index == 1:
        return ChannelDefinition(
            key="topography",
            label="Topography",
            filename_tokens=list(_CLASSIC_TOKENS["topography"]),
        )
    if index == 2:
        return ChannelDefinition(
            key="friction", label="Friction", filename_tokens=list(_CLASSIC_TOKENS["friction"])
        )
    return ChannelDefinition(
        key=f"channel_{index}",
        label=f"Channel {index}",
        filename_tokens=[f"channel{index}", f"ch{index}", f"c{index}"],
    )


def default_channels(count: int = DEFAULT_CHANNEL_COUNT) -> list[ChannelDefinition]:
    if not MIN_CHANNELS <= count <= MAX_CHANNELS:
        raise ValueError(f"channel count must be between {MIN_CHANNELS} and {MAX_CHANNELS}")
    return [default_channel(index) for index in range(1, count + 1)]


def validate_channels(channels: list[ChannelDefinition]) -> list[ChannelDefinition]:
    if not MIN_CHANNELS <= len(channels) <= MAX_CHANNELS:
        raise ValueError(f"a project needs between {MIN_CHANNELS} and {MAX_CHANNELS} channels")
    keys = [c.key for c in channels]
    if len(set(keys)) != len(keys):
        raise ValueError("channel keys must be unique")
    labels = [c.label.casefold() for c in channels]
    if len(set(labels)) != len(labels):
        raise ValueError("channel labels must be unique (case-insensitive)")
    seen: dict[str, str] = {}
    for channel in channels:
        for token in channel.filename_tokens:
            if token in seen and seen[token] != channel.key:
                raise ValueError(f"filename token {token!r} is used by two channels")
            seen[token] = channel.key
    return channels


class SemanticRole(StrEnum):
    MATERIAL_LAYER = "MaterialLayer"
    BOUNDARY = "Boundary"
    UNSPECIFIED = "Unspecified"


class ClassDefinition(BaseModel):
    class_id: int = Field(ge=1)
    stable_key: str
    name: str
    display_color: str
    semantic_role: SemanticRole = SemanticRole.MATERIAL_LAYER
    enabled: bool = True
    #: Explicit downstream export identifier; ``None`` means "use class_id".
    export_id: int | None = None
    description: str = ""

    @field_validator("display_color")
    @classmethod
    def _validate_color(cls, value: str) -> str:
        if not _COLOR_PATTERN.match(value):
            raise ValueError("display_color must be a #rrggbb hex colour")
        return value.lower()

    @field_validator("stable_key")
    @classmethod
    def _validate_key(cls, value: str) -> str:
        if not _STABLE_KEY_PATTERN.match(value):
            raise ValueError("stable_key must match [a-z][a-z0-9_]*")
        return value

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        if len(value) > 100:
            raise ValueError("name is too long (max 100 characters)")
        return value


def normalize_class_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


def stable_key_from_name(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", name.strip().casefold()).strip("_")
    if not key or not key[0].isalpha():
        key = f"class_{key}" if key else "class"
    return key[:64]


class ClassOntology(BaseModel):
    schema_version: int = CLASSES_SCHEMA_VERSION
    classes: list[ClassDefinition] = Field(default_factory=list)
    next_class_id: int = 1

    def by_id(self, class_id: int) -> ClassDefinition | None:
        for definition in self.classes:
            if definition.class_id == class_id:
                return definition
        return None

    def boundary_class(self) -> ClassDefinition | None:
        for definition in self.classes:
            if definition.semantic_role == SemanticRole.BOUNDARY and definition.enabled:
                return definition
        return None

    def validate_unique(self) -> None:
        names = [normalize_class_name(c.name) for c in self.classes]
        if len(set(names)) != len(names):
            raise ValueError("class names must be unique (case-insensitive)")
        keys = [c.stable_key for c in self.classes]
        if len(set(keys)) != len(keys):
            raise ValueError("class stable keys must be unique")
        ids = [c.class_id for c in self.classes]
        if len(set(ids)) != len(ids):
            raise ValueError("class ids must be unique")
        export_ids = [c.export_id if c.export_id is not None else c.class_id for c in self.classes]
        if len(set(export_ids)) != len(export_ids):
            raise ValueError("export ids must be unique")


class ProjectConfig(BaseModel):
    """Project-level overrides of ``annotation`` settings; ``None`` inherits."""

    boundary_mode: BoundaryMode | None = None
    require_overlap_review: bool | None = None
    #: Stable key of the class a flattened-mask import proposes when the
    #: project has no ``buffer`` class. ``None``
    #: means "ask the user". Additive, optional: older project.json files
    #: load unchanged.
    default_import_class_key: str | None = None


def find_default_import_class(
    ontology: ClassOntology, config: ProjectConfig | None = None
) -> ClassDefinition | None:
    """Class proposed for a simple flattened-mask import.

    Order: an enabled class keyed/named ``buffer``; then the project's
    configured ``default_import_class_key``; otherwise ``None`` so the UI
    must ask (nothing scientific is invented silently).
    """
    for definition in ontology.classes:
        if definition.enabled and (
            definition.stable_key == "buffer" or normalize_class_name(definition.name) == "buffer"
        ):
            return definition
    if config is not None and config.default_import_class_key:
        for definition in ontology.classes:
            if definition.enabled and definition.stable_key == config.default_import_class_key:
                return definition
    return None


class UiState(BaseModel):
    last_dataset_id: str | None = None
    #: Key of the displayed source channel. Older files call it ``modality``;
    #: the alias keeps them readable (ui_state.json carries no schema version).
    channel: str = Field(default="topography", validation_alias=AliasChoices("channel", "modality"))
    #: Seed-fill tool options (upgrade: fill tool). Additive, optional.
    fill_gap_px: int = Field(default=1, ge=0, le=20)
    fill_connectivity: Literal[4, 8] = 4
    selected_region_id: str | None = None
    selected_class_id: int | None = None
    tool: str = "select"
    zoom: float | None = None
    pan_x: float | None = None
    pan_y: float | None = None
    overlay_opacity: float = 0.55
    brush_radius: int = 10
    show_overlaps: bool = True
    show_missing: bool = True
    selected_backend_id: str | None = None


class HistoryEntry(BaseModel):
    kind: Literal["import_pairs", "import_tswat", "import_lossy", "export", "replace_source"]
    entry_id: str
    at: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class Project(BaseModel):
    schema_version: int = PROJECT_SCHEMA_VERSION
    project_id: str
    name: str
    created_at: str
    modified_at: str
    revision: int = 1
    status: Literal["active", "archived"] = "active"
    config: ProjectConfig = Field(default_factory=ProjectConfig)
    #: Source channels every dataset carries, in display order (schema v2).
    channels: list[ChannelDefinition] = Field(default_factory=default_channels)
    dataset_order: list[str] = Field(default_factory=list)
    import_history: list[HistoryEntry] = Field(default_factory=list)
    export_history: list[HistoryEntry] = Field(default_factory=list)

    @field_validator("channels")
    @classmethod
    def _validate_channels(cls, value: list[ChannelDefinition]) -> list[ChannelDefinition]:
        return validate_channels(value)

    @property
    def channel_keys(self) -> list[str]:
        return [c.key for c in self.channels]

    def channel(self, key: str) -> ChannelDefinition | None:
        for definition in self.channels:
            if definition.key == key:
                return definition
        return None


class ProjectSummary(BaseModel):
    project_id: str
    name: str
    created_at: str
    modified_at: str
    revision: int
    status: str
    dataset_count: int
    class_count: int
    channel_count: int = DEFAULT_CHANNEL_COUNT
    first_dataset_id: str | None = None
    first_channel_key: str = "topography"


class LockInfo(BaseModel):
    owner_id: str
    owner_label: str
    acquired_at: str
    heartbeat_at: str
    pid: int
    hostname: str


class LockState(BaseModel):
    locked: bool
    owned_by_caller: bool
    stale: bool
    info: LockInfo | None = None
