# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Pairing-key extraction and pairing preview, N channels.

Given filenames such as ``242_topography2.tiff`` / ``242_friction2.tiff``
or ``239_topography.tiff`` / ``239_friction.tiff`` the key is the filename
with the project's channel tokens and separators removed (``242``,
``239``). Keys are never guessed from lexicographic order unless *no* file
yields a key, and then only with explicit user confirmation
(``pairing_method="order"``).

A dataset needs exactly one file per project channel; a row is ``ok`` only
when every channel slot is filled by one decodable file and all files
agree on dimensions and calibration.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Literal

from afmlabel.domain.project import ChannelDefinition, ChannelKey, default_channels

_SEPARATORS = re.compile(r"[\s_\-.]+")

PairStatus = Literal[
    "ok",
    "missing",
    "ambiguous",
    "invalid",
    "dimension_mismatch",
    "calibration_mismatch",
]


def channel_tokens(channels: Sequence[ChannelDefinition]) -> tuple[str, ...]:
    """All filename tokens of all channels, longest first."""
    return tuple(
        sorted({t for channel in channels for t in channel.filename_tokens}, key=len, reverse=True)
    )


def extract_pairing_key(
    filename: str,
    channel: ChannelKey | None = None,
    *,
    tokens: Sequence[str] | None = None,
) -> str:
    """Return the channel-independent key, or ``""`` when nothing is left.

    ``tokens`` defaults to the classic topography/friction vocabulary so the
    historical behaviour (and tests) hold for two-channel projects.
    """
    words = tuple(tokens) if tokens is not None else channel_tokens(default_channels(2))
    stem = PurePath(filename).stem
    lowered = stem.casefold()
    parts = [t for t in _SEPARATORS.split(lowered) if t]
    kept: list[str] = []
    for token in parts:
        stripped = token
        for word in words:
            if stripped == word:
                stripped = ""
                break
            if stripped.startswith(word) and stripped[len(word) :].isdigit():
                # "topography2" -> variant suffix "2" is kept so 242_topography2
                # still pairs with 242_friction2 but not with 242_friction.
                stripped = stripped[len(word) :]
                break
            if stripped.endswith(word) and stripped[: -len(word)].isdigit():
                stripped = stripped[: -len(word)]
                break
        if stripped:
            kept.append(stripped)
    return "_".join(kept)


@dataclass
class BatchFile:
    file_id: str
    channel: ChannelKey
    original_filename: str
    pairing_key: str
    width: int | None = None
    height: int | None = None
    error: str | None = None
    calibration_signature: str | None = None

    # Backwards-compatible spelling used by older call sites.
    @property
    def modality(self) -> ChannelKey:
        return self.channel


@dataclass
class PairRow:
    pairing_key: str
    status: PairStatus
    #: Exactly one entry per project channel (``None`` = slot not filled).
    files: dict[ChannelKey, BatchFile | None] = field(default_factory=dict)
    candidates: list[BatchFile] = field(default_factory=list)
    message: str = ""
    missing_channels: list[ChannelKey] = field(default_factory=list)

    @property
    def committable(self) -> bool:
        return self.status == "ok"

    @property
    def topography(self) -> BatchFile | None:
        return self.files.get("topography")

    @property
    def friction(self) -> BatchFile | None:
        return self.files.get("friction")

    def file_ids(self) -> dict[ChannelKey, str]:
        return {key: f.file_id for key, f in self.files.items() if f is not None}


@dataclass
class PairingPreview:
    rows: list[PairRow]
    method: Literal["key", "order"]
    ok_count: int
    problem_count: int
    order_based_fallback_available: bool
    channel_keys: list[ChannelKey] = field(default_factory=list)


def _group(files: list[BatchFile], channel: ChannelKey) -> dict[str, list[BatchFile]]:
    groups: dict[str, list[BatchFile]] = {}
    for item in files:
        if item.channel != channel:
            continue
        groups.setdefault(item.pairing_key, []).append(item)
    return groups


def _check_row(row: PairRow, labels: dict[ChannelKey, str]) -> None:
    """Row has one file per channel: validate decode, dimensions, calibration."""
    present = [(key, f) for key, f in row.files.items() if f is not None]
    for _key, item in present:
        if item.error:
            row.status = "invalid"
            row.message = item.error
            return
    first_key, first = present[0]
    for key, item in present[1:]:
        if (first.width, first.height) != (item.width, item.height):
            row.status = "dimension_mismatch"
            row.message = (
                f"{labels[first_key]} {first.width}x{first.height} vs "
                f"{labels[key]} {item.width}x{item.height}"
            )
            return
    signatures = [(key, f.calibration_signature) for key, f in present if f.calibration_signature]
    for key, signature in signatures[1:]:
        if signature != signatures[0][1]:
            row.status = "calibration_mismatch"
            row.message = (
                f"calibration {signatures[0][1]} ({labels[signatures[0][0]]}) vs "
                f"{signature} ({labels[key]})"
            )
            return
    row.status = "ok"


def build_preview(
    files: list[BatchFile],
    *,
    channels: Sequence[ChannelDefinition] | None = None,
    allow_order_based: bool = False,
) -> PairingPreview:
    definitions = list(channels) if channels is not None else default_channels(2)
    keys_order = [c.key for c in definitions]
    labels = {c.key: c.label for c in definitions}
    keys_all_empty = all(not f.pairing_key for f in files) and bool(files)
    method: Literal["key", "order"] = "key"
    rows: list[PairRow] = []

    if keys_all_empty and allow_order_based:
        method = "order"
        per_channel = {
            key: sorted((f for f in files if f.channel == key), key=lambda f: f.original_filename)
            for key in keys_order
        }
        longest = max((len(v) for v in per_channel.values()), default=0)
        for index in range(longest):
            row = PairRow(pairing_key=f"pair_{index + 1:03d}", status="ok")
            for key in keys_order:
                items = per_channel[key]
                row.files[key] = items[index] if index < len(items) else None
            _finish_row(row, keys_order, labels)
            rows.append(row)
    else:
        groups = {key: _group(files, key) for key in keys_order}
        all_keys = sorted({k for g in groups.values() for k in g}, key=_natural_key)
        for pairing_key in all_keys:
            row = PairRow(pairing_key=pairing_key or "(no key)", status="ok")
            per_channel = {key: groups[key].get(pairing_key, []) for key in keys_order}
            if not pairing_key:
                # An empty key never pairs implicitly: that would be silent
                # order-based pairing.
                row.status = "ambiguous"
                row.candidates = [f for key in keys_order for f in per_channel[key]]
                row.message = "no identifier could be extracted from the filename"
                rows.append(row)
                continue
            if any(len(items) > 1 for items in per_channel.values()):
                row.status = "ambiguous"
                row.candidates = [f for key in keys_order for f in per_channel[key]]
                counts = ", ".join(
                    f"{len(per_channel[key])} {labels[key]}"
                    for key in keys_order
                    if per_channel[key]
                )
                row.message = f"{counts} files share key {pairing_key!r}"
                rows.append(row)
                continue
            for key in keys_order:
                row.files[key] = per_channel[key][0] if per_channel[key] else None
            _finish_row(row, keys_order, labels)
            rows.append(row)

    ok = sum(1 for r in rows if r.status == "ok")
    return PairingPreview(
        rows=rows,
        method=method,
        ok_count=ok,
        problem_count=len(rows) - ok,
        order_based_fallback_available=keys_all_empty and not allow_order_based,
        channel_keys=keys_order,
    )


def _finish_row(row: PairRow, keys_order: list[ChannelKey], labels: dict[ChannelKey, str]) -> None:
    missing = [key for key in keys_order if row.files.get(key) is None]
    if missing:
        row.status = "missing"
        row.missing_channels = missing
        row.message = "missing " + ", ".join(labels[key] for key in missing)
        return
    _check_row(row, labels)


def _natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", value)]
