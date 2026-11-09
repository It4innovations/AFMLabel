# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Pixel-exact raster writers shared by the export profiles.

* ``write_label_raster``: the machine-readable mask (uint16, one scalar per
  pixel) as PNG or uncompressed TIFF. Values are never rescaled.
* ``color_mask``/``write_color_mask``: the human-visible visualisation of the
  *same* final composition using the exact project class colours, RGB,
  no alpha, no anti-aliasing. Value ``0`` (only present under the explicit
  ``zero`` Boundary policy) renders as black and is listed in the legend.
* ``export_stems``: deterministic, filesystem-safe sample names derived from
  the pairing key that itself comes from the original filenames.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from afmlabel.domain.project import ClassOntology

RasterFormat = str  # "png" | "tiff"

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def raster_suffix(raster_format: RasterFormat) -> str:
    return ".png" if raster_format == "png" else ".tif"


def write_label_raster(path: Path, array: np.ndarray, raster_format: RasterFormat) -> None:
    """Write ``array`` (integer class/instance ids) losslessly as uint16."""
    if array.min() < 0 or array.max() > 65535:
        raise ValueError("mask values must fit into uint16")
    image = Image.fromarray(array.astype(np.uint16))
    if raster_format == "png":
        image.save(path, format="PNG")
    else:
        image.save(path, format="TIFF", compression=None)


def read_label_raster(path: Path) -> np.ndarray:
    """Read a mask written by :func:`write_label_raster` back as int32."""
    with Image.open(path) as image:
        array = np.array(image)
    if array.ndim == 3:
        array = array[:, :, 0]
    return array.astype(np.int32)


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = int(color.lstrip("#"), 16)
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def color_legend(ontology: ClassOntology, export_ids: dict[int, int]) -> list[dict[str, Any]]:
    """Legend entries keyed by the *export value* found in the machine mask."""
    legend: list[dict[str, Any]] = [
        {
            "export_value": 0,
            "class_id": None,
            "stable_key": None,
            "name": "unlabelled / ignored (boundary_policy=zero only)",
            "display_color": "#000000",
            "rgb": [0, 0, 0],
            "semantic_role": None,
        }
    ]
    for definition in ontology.classes:
        legend.append(
            {
                "export_value": export_ids[definition.class_id],
                "class_id": definition.class_id,
                "stable_key": definition.stable_key,
                "name": definition.name,
                "display_color": definition.display_color,
                "rgb": list(hex_to_rgb(definition.display_color)),
                "semantic_role": definition.semantic_role.value,
            }
        )
    legend.sort(key=lambda entry: entry["export_value"])
    return legend


def color_mask(label_map: np.ndarray, legend: list[dict[str, Any]]) -> np.ndarray:
    """(H, W, 3) uint8 RGB where every pixel is exactly one legend colour."""
    lookup: dict[int, tuple[int, int, int]] = {
        int(entry["export_value"]): tuple(entry["rgb"]) for entry in legend
    }
    rgb = np.zeros((*label_map.shape, 3), dtype=np.uint8)
    unknown = set(np.unique(label_map).tolist()) - set(lookup)
    if unknown:
        raise ValueError(f"mask values without a class colour: {sorted(unknown)}")
    for value, color in lookup.items():
        rgb[label_map == value] = color
    return rgb


def write_color_mask(path: Path, label_map: np.ndarray, legend: list[dict[str, Any]]) -> None:
    Image.fromarray(color_mask(label_map, legend), mode="RGB").save(path, format="PNG")


def safe_stem(value: str) -> str:
    stem = _UNSAFE.sub("_", value.strip()).strip("._")
    return stem[:120] or "dataset"


def first_digit_run(stem: str) -> str:
    """What the NanoUNet loader's ``extract_id`` returns for ``stem``."""
    match = re.search(r"\d+", stem)
    return match.group(0) if match else stem


def export_stems(pairing_keys: list[str], *, loader_id_matching: bool) -> list[str]:
    """Deterministic sample names for one export run.

    The pairing key is the identifier the importer derived from the original
    topography/friction filenames (``242_2_topography.tiff`` -> ``242_2``),
    so it is the meaningful original stem. It is sanitised, and duplicates
    (same key from different directories) are disambiguated with a stable
    ``~N`` suffix in input order.

    With ``loader_id_matching`` (NanoUNet) the downstream loader matches
    files by the *first digit run* of the stem. If two stems would collide
    under that rule (``242_2`` and ``242_3`` both give ``242``), every stem
    of the run gets a zero-padded sequence prefix so the loader id becomes
    the sequence number while the original stem is preserved after it.
    """
    stems: list[str] = []
    seen: dict[str, int] = {}
    for key in pairing_keys:
        stem = safe_stem(key)
        count = seen.get(stem, 0)
        seen[stem] = count + 1
        stems.append(stem if count == 0 else f"{stem}~{count + 1}")
    if loader_id_matching:
        ids = [first_digit_run(stem) for stem in stems]
        if len(set(ids)) != len(ids):
            width = max(3, len(str(len(stems))))
            stems = [f"{index + 1:0{width}d}_{stem}" for index, stem in enumerate(stems)]
    return stems
