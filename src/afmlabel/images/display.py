# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Deterministic display rendering of scientific rasters.

Display normalisation is separate from the immutable source: a uint16 or
float channel is stretched between its 0.5th and 99.5th percentile into
8-bit grayscale; RGB sources are passed through. The same function feeds
the browser canvas, thumbnails and SAM preprocessing, so
coordinates never shift and the mapping is reproducible from the recorded
``DisplayParams``.
"""

from __future__ import annotations

import io
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from afmlabel.persistence.atomic import atomic_write_bytes


@dataclass(frozen=True)
class DisplayParams:
    method: str = "percentile"
    low_percentile: float = 0.5
    high_percentile: float = 99.5
    gamma: float = 1.0
    version: int = 1

    def as_dict(self) -> dict[str, float | str | int]:
        return asdict(self)


DEFAULT_DISPLAY = DisplayParams()


def to_display_uint8(array: np.ndarray, params: DisplayParams = DEFAULT_DISPLAY) -> np.ndarray:
    """Return an (H, W) uint8 grayscale or (H, W, 3) uint8 RGB array."""
    if array.ndim == 3:
        if array.shape[2] >= 3:
            rgb = array[:, :, :3]
            if rgb.dtype == np.uint8:
                return np.ascontiguousarray(rgb)
            return np.stack([_stretch(rgb[:, :, c], params) for c in range(3)], axis=-1)
        array = array[:, :, 0]
    if array.dtype == np.uint8 and params.method == "identity":
        return array
    return _stretch(array, params)


def _stretch(channel: np.ndarray, params: DisplayParams) -> np.ndarray:
    data = channel.astype(np.float64)
    finite = np.isfinite(data)
    if not finite.any():
        return np.zeros(channel.shape, dtype=np.uint8)
    low, high = np.percentile(data[finite], [params.low_percentile, params.high_percentile])
    if high <= low:
        low, high = float(np.nanmin(data[finite])), float(np.nanmax(data[finite]))
    if high <= low:
        return np.zeros(channel.shape, dtype=np.uint8)
    scaled = np.clip((data - low) / (high - low), 0.0, 1.0)
    scaled[~finite] = 0.0
    if params.gamma != 1.0:
        scaled = np.power(scaled, 1.0 / params.gamma)
    return np.round(scaled * 255.0).astype(np.uint8)


def to_rgb_uint8(array: np.ndarray, params: DisplayParams = DEFAULT_DISPLAY) -> np.ndarray:
    """(H, W, 3) uint8, replicating a grayscale channel -- the SAM input form."""
    display = to_display_uint8(array, params)
    if display.ndim == 2:
        return np.repeat(display[:, :, None], 3, axis=2)
    return display


def encode_png(display: np.ndarray) -> bytes:
    image = Image.fromarray(display, mode="L" if display.ndim == 2 else "RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def encode_thumbnail_png(display: np.ndarray, size: int = 192) -> bytes:
    image = Image.fromarray(display, mode="L" if display.ndim == 2 else "RGB")
    image.thumbnail((size, size))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", compress_level=6)
    return buffer.getvalue()


def render_display_cache(array: np.ndarray, display_path: Path, thumbnail_path: Path) -> None:
    display = to_display_uint8(array)
    atomic_write_bytes(display_path, encode_png(display), fsync=False)
    atomic_write_bytes(thumbnail_path, encode_thumbnail_png(display), fsync=False)
