# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import numpy as np
import pytest

from afmlabel.persistence.rle import (
    bounding_box,
    decode_coco,
    decode_rows,
    encode_coco,
    encode_rows,
    label_components,
    polygon_to_mask,
    split_components,
)


def _mask() -> np.ndarray:
    mask = np.zeros((16, 20), dtype=bool)
    mask[2:6, 3:9] = True
    mask[3:4, 5:6] = False  # hole
    mask[10:14, 12:18] = True  # second component
    mask[15, 0] = True  # single pixel touching the border
    return mask


def test_coco_round_trip_preserves_holes_and_components() -> None:
    mask = _mask()
    rle = encode_coco(mask)
    assert rle.size == [16, 20]
    assert np.array_equal(decode_coco(rle), mask)


def test_row_rle_round_trip_and_validation() -> None:
    mask = _mask()
    counts = encode_rows(mask)
    assert counts[0] == 2 * 20 + 3  # background run before the first pixel
    assert np.array_equal(decode_rows(counts, 16, 20), mask)
    starts_true = np.ones((2, 2), dtype=bool)
    assert encode_rows(starts_true) == [0, 4]
    assert encode_rows(np.zeros((2, 2), dtype=bool)) == [4]
    with pytest.raises(ValueError):
        decode_rows([3], 2, 2)
    with pytest.raises(ValueError):
        decode_rows([-1, 5], 2, 2)


def test_polygon_rasterisation() -> None:
    mask = polygon_to_mask([(2, 2), (10, 2), (10, 10), (2, 10)], 12, 12)
    assert mask[5, 5] and not mask[0, 0] and not mask[11, 11]
    with pytest.raises(ValueError):
        polygon_to_mask([(0, 0), (1, 1)], 4, 4)


def test_bounding_box() -> None:
    assert bounding_box(_mask()) == [0, 2, 18, 14]
    assert bounding_box(np.zeros((3, 3), dtype=bool)) == [0, 0, 0, 0]


def test_component_labelling_and_split() -> None:
    labels, count = label_components(_mask())
    assert count == 3
    assert labels.max() == 3
    parts = split_components(_mask())
    assert [int(p.sum()) for p in parts] == [24, 23, 1]
    assert np.array_equal(np.logical_or.reduce(parts), _mask())
    # Diagonal neighbours are not connected (4-connectivity).
    diag = np.eye(4, dtype=bool)
    assert label_components(diag)[1] == 4
    assert label_components(np.zeros((2, 2), dtype=bool))[1] == 0
