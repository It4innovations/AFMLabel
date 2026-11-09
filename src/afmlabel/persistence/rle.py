# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Lossless mask codecs and raster helpers.

* **Canonical storage**: COCO-compatible compressed RLE via ``pycocotools``
  (exact, compact, supports holes and disconnected components, and matches
  what downstream COCO tooling expects byte for byte).
* **Browser transport**: row-major run lengths (``rle_rows``), trivially
  encoded/decoded in JavaScript, alternating background/foreground starting
  with background.
* **Polygons** are rasterised with Pillow so the server, not the browser,
  defines pixel membership.
* **Connected components** use a run-based union-find (no SciPy
  dependency); it also backs overlap-group detection.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from PIL import Image, ImageDraw
from pycocotools import mask as _mask_utils

from afmlabel.domain.region import RleMask


def encode_coco(mask: np.ndarray) -> RleMask:
    if mask.ndim != 2:
        raise ValueError(f"expected a 2D mask, got shape {mask.shape}")
    fortran = np.asfortranarray(mask.astype(np.uint8))
    encoded = _mask_utils.encode(fortran)
    counts = encoded["counts"]
    if isinstance(counts, bytes):
        counts = counts.decode("ascii")
    height, width = encoded["size"]
    return RleMask(size=[int(height), int(width)], counts=counts)


def decode_coco(rle: RleMask) -> np.ndarray:
    coco: Any = {"size": list(rle.size), "counts": rle.counts.encode("ascii")}
    decoded = _mask_utils.decode(coco)
    result: np.ndarray = decoded.astype(bool)
    return result


def encode_rows(mask: np.ndarray) -> list[int]:
    """Row-major run lengths, alternating 0/1 runs, starting with a 0 run."""
    flat = np.asarray(mask, dtype=bool).ravel()
    if flat.size == 0:
        return []
    changes = np.flatnonzero(flat[1:] != flat[:-1]) + 1
    boundaries = np.concatenate(([0], changes, [flat.size]))
    runs = np.diff(boundaries).tolist()
    if flat[0]:
        runs.insert(0, 0)
    return [int(r) for r in runs]


def decode_rows(counts: list[int], height: int, width: int) -> np.ndarray:
    total = height * width
    if sum(counts) != total:
        raise ValueError(f"run lengths sum to {sum(counts)}, expected {total}")
    if any(c < 0 for c in counts):
        raise ValueError("run lengths must be non-negative")
    flat = np.zeros(total, dtype=bool)
    position = 0
    value = False
    for run in counts:
        if value and run:
            flat[position : position + run] = True
        position += run
        value = not value
    return flat.reshape(height, width)


def polygon_to_mask(points: list[tuple[float, float]], height: int, width: int) -> np.ndarray:
    if len(points) < 3:
        raise ValueError("a polygon needs at least three points")
    image = Image.new("L", (width, height), 0)
    ImageDraw.Draw(image).polygon([(float(x), float(y)) for x, y in points], fill=1, outline=1)
    result: np.ndarray = np.asarray(image, dtype=bool)
    return result


def bounding_box(mask: np.ndarray) -> list[int]:
    """``[x, y, w, h]`` of the true pixels, or ``[0, 0, 0, 0]`` when empty."""
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return [0, 0, 0, 0]
    return [int(cols[0]), int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)]


def label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-connected component labelling; returns ``(labels, count)`` with
    labels ``1..count`` and ``0`` for background."""
    mask = np.asarray(mask, dtype=bool)
    height, width = mask.shape
    labels = np.zeros((height, width), dtype=np.int32)
    if not mask.any():
        return labels, 0
    padded = np.zeros((height, width + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    deltas = np.diff(padded, axis=1)
    start_rows, start_cols = np.nonzero(deltas == 1)
    _end_rows, end_cols = np.nonzero(deltas == -1)
    run_count = len(start_rows)
    parent = np.arange(run_count, dtype=np.int64)

    def find(run: int) -> int:
        root = run
        while parent[root] != root:
            root = int(parent[root])
        while parent[run] != root:
            parent[run], run = root, int(parent[run])
        return root

    row_boundaries = np.searchsorted(start_rows, np.arange(height + 1))
    for row in range(1, height):
        above, above_end = int(row_boundaries[row - 1]), int(row_boundaries[row])
        current, current_end = int(row_boundaries[row]), int(row_boundaries[row + 1])
        while above < above_end and current < current_end:
            if start_cols[above] < end_cols[current] and start_cols[current] < end_cols[above]:
                ra, rc = find(above), find(current)
                if ra != rc:
                    parent[max(ra, rc)] = min(ra, rc)
            if end_cols[above] < end_cols[current]:
                above += 1
            else:
                current += 1
    roots = np.array([find(i) for i in range(run_count)], dtype=np.int64)
    unique_roots, component_ids = np.unique(roots, return_inverse=True)
    for run in range(run_count):
        labels[start_rows[run], start_cols[run] : end_cols[run]] = component_ids[run] + 1
    return labels, int(len(unique_roots))


def split_components(mask: np.ndarray) -> list[np.ndarray]:
    """Component masks ordered by descending area."""
    labels, count = label_components(mask)
    parts = [labels == index for index in range(1, count + 1)]
    parts.sort(key=lambda part: int(part.sum()), reverse=True)
    return parts
