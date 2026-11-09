# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Golden raster tests for the export writers: PNG/TIFF
round-trips, colour visualisation from exact class colours, both derived
from the same label map, and deterministic original-filename stems."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from afmlabel.domain.project import ClassDefinition, ClassOntology
from afmlabel.exports.rasters import (
    color_legend,
    color_mask,
    export_stems,
    first_digit_run,
    read_label_raster,
    safe_stem,
    write_color_mask,
    write_label_raster,
)


def ontology() -> ClassOntology:
    return ClassOntology(
        classes=[
            ClassDefinition(class_id=1, stable_key="slg", name="SLG", display_color="#fa3253"),
            ClassDefinition(
                class_id=2, stable_key="buffer", name="buffer", display_color="#00a398", export_id=7
            ),
        ],
        next_class_id=3,
    )


def golden_label_map() -> np.ndarray:
    label = np.zeros((8, 8), dtype=np.int32)
    label[:4, :] = 1
    label[4:, :] = 7
    label[0, 0] = 0  # only under boundary_policy=zero
    return label


@pytest.mark.parametrize("raster_format", ["png", "tiff"])
def test_label_raster_round_trip_is_pixel_exact(tmp_path: Path, raster_format: str) -> None:
    label = golden_label_map()
    label[7, 7] = 65535
    path = tmp_path / f"mask.{raster_format}"
    write_label_raster(path, label, raster_format)
    with Image.open(path) as image:
        assert image.mode in ("I;16", "I", "I;16B")
    back = read_label_raster(path)
    assert np.array_equal(back, label)


def test_label_raster_refuses_values_outside_uint16(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_label_raster(tmp_path / "x.png", np.array([[70000]]), "png")


def test_color_mask_uses_exact_class_colors_and_same_composition(tmp_path: Path) -> None:
    ont = ontology()
    legend = color_legend(ont, {1: 1, 2: 7})
    assert [e["export_value"] for e in legend] == [0, 1, 7]
    label = golden_label_map()
    rgb = color_mask(label, legend)
    assert rgb.dtype == np.uint8 and rgb.shape == (8, 8, 3)
    assert tuple(rgb[1, 1]) == (0xFA, 0x32, 0x53)
    assert tuple(rgb[5, 5]) == (0x00, 0xA3, 0x98)
    assert tuple(rgb[0, 0]) == (0, 0, 0)
    # Every pixel is exactly one legend colour (no anti-aliasing).
    colours = {tuple(px) for px in rgb.reshape(-1, 3)}
    assert colours == {(0xFA, 0x32, 0x53), (0x00, 0xA3, 0x98), (0, 0, 0)}
    path = tmp_path / "mask_color.png"
    write_color_mask(path, label, legend)
    with Image.open(path) as image:
        assert image.mode == "RGB"
        decoded = np.array(image)
    assert np.array_equal(decoded, rgb)
    # The colour raster maps back to the label map through the legend.
    reverse = {tuple(e["rgb"]): e["export_value"] for e in legend}
    recovered = np.vectorize(lambda r, g, b: reverse[(r, g, b)])(
        decoded[:, :, 0], decoded[:, :, 1], decoded[:, :, 2]
    )
    assert np.array_equal(recovered, label)


def test_color_mask_refuses_unknown_values() -> None:
    legend = color_legend(ontology(), {1: 1, 2: 7})
    with pytest.raises(ValueError):
        color_mask(np.array([[3]]), legend)


def test_export_stems_are_deterministic_and_loader_safe() -> None:
    assert safe_stem("242_2") == "242_2"
    assert safe_stem("my sample/242 (b).tif") == "my_sample_242_b_.tif"
    assert first_digit_run("242_2") == "242" and first_digit_run("abc") == "abc"
    keys = ["242_2", "243", "abc"]
    assert export_stems(keys, loader_id_matching=True) == ["242_2", "243", "abc"]
    assert export_stems(keys, loader_id_matching=True) == export_stems(
        keys, loader_id_matching=True
    )
    # Same key twice (different directories): stable suffix, no overwrite.
    assert export_stems(["242", "242"], loader_id_matching=False) == ["242", "242~2"]
    # Loader collision on the first digit run -> sequence prefix for the run.
    assert export_stems(["242_2", "242_3"], loader_id_matching=True) == [
        "001_242_2",
        "002_242_3",
    ]
    assert export_stems(["242_2", "242_3"], loader_id_matching=False) == ["242_2", "242_3"]
