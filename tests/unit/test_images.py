# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from afmlabel.domain.dataset import Calibration
from afmlabel.images.decode import (
    ImageDecodeError,
    calibrations_compatible,
    decode_image,
    sha256_of_file,
)
from afmlabel.images.display import DisplayParams, encode_png, to_display_uint8, to_rgb_uint8
from tests.fixtures.images import synthetic_topography, write_png, write_tiff


def test_decode_uint16_tiff_preserves_data(tmp_path: Path) -> None:
    array = synthetic_topography(32, 48)
    path = write_tiff(tmp_path / "t.tiff", array)
    decoded = decode_image(path)
    assert decoded.width == 48 and decoded.height == 32
    assert decoded.dtype == "uint16" and decoded.bit_depth == 16 and decoded.channels == 1
    assert decoded.format == "tiff"
    assert not decoded.lossy_format
    assert np.array_equal(decoded.array, array)
    assert decoded.calibration.pixel_size_x is None
    assert len(sha256_of_file(path)) == 64


def test_decode_lzw_rgb_tiff_like_the_cvat_exports(tmp_path: Path) -> None:
    rgb = np.random.default_rng(0).integers(0, 255, size=(20, 30, 3), dtype=np.uint8)
    path = write_tiff(tmp_path / "rgb.tiff", rgb, compression="tiff_lzw")
    decoded = decode_image(path)
    assert decoded.channels == 3 and decoded.dtype == "uint8"
    assert decoded.metadata["compression"] == "lzw"
    assert np.array_equal(decoded.array, rgb)


def test_decode_png_and_jpeg_flags(tmp_path: Path) -> None:
    gray = np.random.default_rng(1).integers(0, 255, size=(16, 16), dtype=np.uint8)
    png = decode_image(write_png(tmp_path / "g.png", gray))
    assert png.format == "png" and not png.lossy_format
    Image.fromarray(gray).save(tmp_path / "g.jpg", quality=90)
    jpeg = decode_image(tmp_path / "g.jpg")
    assert jpeg.lossy_format


def test_decode_rejects_garbage_and_unknown_extensions(tmp_path: Path) -> None:
    (tmp_path / "x.tiff").write_bytes(b"not a tiff")
    with pytest.raises(ImageDecodeError) as excinfo:
        decode_image(tmp_path / "x.tiff")
    assert excinfo.value.code == "IMAGE_DECODE_FAILED"
    (tmp_path / "x.bmp").write_bytes(b"BM")
    with pytest.raises(ImageDecodeError):
        decode_image(tmp_path / "x.bmp")


def test_tiff_resolution_tags_become_calibration(tmp_path: Path) -> None:
    array = synthetic_topography(8, 8)
    image = Image.fromarray(array)
    image.save(tmp_path / "cal.tiff", format="TIFF", dpi=(100, 200), resolution_unit=2)
    decoded = decode_image(tmp_path / "cal.tiff")
    assert decoded.calibration.source == "tiff_resolution"
    assert decoded.calibration.unit == "inch"
    assert decoded.calibration.pixel_size_x == pytest.approx(0.01)
    assert decoded.calibration.pixel_size_y == pytest.approx(0.005)


def test_calibration_compatibility() -> None:
    a = Calibration(pixel_size_x=1.0, pixel_size_y=1.0, unit="nm")
    b = Calibration(pixel_size_x=1.0000001, pixel_size_y=1.0, unit="nm")
    c = Calibration(pixel_size_x=2.0, pixel_size_y=1.0, unit="nm")
    assert calibrations_compatible(a, b)
    assert not calibrations_compatible(a, c)
    assert calibrations_compatible(a, Calibration())
    assert not calibrations_compatible(
        a, Calibration(pixel_size_x=1.0, pixel_size_y=1.0, unit="um")
    )


def test_display_normalisation_is_deterministic_and_bounded() -> None:
    array = synthetic_topography(40, 40)
    first = to_display_uint8(array)
    second = to_display_uint8(array)
    assert first.dtype == np.uint8 and first.shape == (40, 40)
    assert np.array_equal(first, second)
    assert first.min() == 0 and first.max() == 255
    rgb = to_rgb_uint8(array)
    assert rgb.shape == (40, 40, 3)
    assert np.array_equal(rgb[:, :, 0], first)
    assert encode_png(first).startswith(b"\x89PNG")


def test_display_handles_constant_and_nan_inputs() -> None:
    assert to_display_uint8(np.full((4, 4), 7, dtype=np.uint16)).max() == 0
    array = np.full((4, 4), np.nan, dtype=np.float32)
    assert to_display_uint8(array).max() == 0
    array[0, 0], array[1, 1] = 0.0, 1.0
    out = to_display_uint8(array, DisplayParams(low_percentile=0, high_percentile=100))
    assert out[1, 1] == 255 and out[2, 2] == 0


def test_rgb_uint8_passes_through() -> None:
    rgb = np.random.default_rng(3).integers(0, 255, size=(5, 5, 3), dtype=np.uint8)
    assert np.array_equal(to_display_uint8(rgb), rgb)
