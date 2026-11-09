# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Synthetic AFM-like fixtures written on demand (nothing binary is committed)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image


def synthetic_topography(height: int = 64, width: int = 64, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:height, :width].astype(np.float64)
    base = (xx / max(width - 1, 1)) * 30000 + (yy / max(height - 1, 1)) * 20000
    noise = rng.normal(0, 500, size=(height, width))
    return np.clip(base + noise, 0, 65535).astype(np.uint16)


def synthetic_friction(height: int = 64, width: int = 64, seed: int = 2) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:height, :width].astype(np.float64)
    ring = np.exp(-((np.hypot(xx - width / 2, yy - height / 2) - width / 4) ** 2) / 20.0)
    noise = rng.normal(0, 0.02, size=(height, width))
    return np.clip((ring + noise) * 60000, 0, 65535).astype(np.uint16)


def write_tiff(path: Path, array: np.ndarray, *, compression: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.fromarray(array)
    if compression:
        image.save(path, format="TIFF", compression=compression)
    else:
        image.save(path, format="TIFF")
    return path


def write_png(path: Path, array: np.ndarray) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array).save(path, format="PNG")
    return path


def write_pair(directory: Path, key: str, height: int = 64, width: int = 64) -> tuple[Path, Path]:
    topo = write_tiff(directory / f"{key}_topography.tiff", synthetic_topography(height, width))
    fric = write_tiff(directory / f"{key}_friction.tiff", synthetic_friction(height, width))
    return topo, fric
