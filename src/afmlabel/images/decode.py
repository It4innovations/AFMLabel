# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Decode AFM raster sources without altering them.

Pillow decodes TIFF (including LZW, 8/16-bit, RGB), PNG and JPEG; tifffile
is the fallback for scientific TIFF variants Pillow cannot read (float32,
multi-sample). The original file is never modified; display normalisation
lives in :mod:`afmlabel.images.display`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import numpy as np
from PIL import Image, UnidentifiedImageError

from afmlabel.domain.dataset import Calibration
from afmlabel.errors import AppError

Image.MAX_IMAGE_PIXELS = 64_000_000  # 8k x 8k; AFM scans are far smaller

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({".tif", ".tiff", ".png", ".jpg", ".jpeg"})
LOSSY_EXTENSIONS: frozenset[str] = frozenset({".jpg", ".jpeg"})

_TIFF_COMPRESSION_NAMES = {1: "none", 5: "lzw", 7: "jpeg", 8: "deflate", 32773: "packbits"}


@dataclass
class DecodedImage:
    array: np.ndarray
    width: int
    height: int
    dtype: str
    bit_depth: int
    channels: int
    format: str
    lossy_format: bool
    calibration: Calibration
    metadata: dict[str, Any] = field(default_factory=dict)


class ImageDecodeError(AppError):
    status_code = 422

    def __init__(self, filename: str, reason: str) -> None:
        super().__init__(
            "IMAGE_DECODE_FAILED",
            f"{filename}: {reason}",
            target={"filename": filename},
            recovery="upload an uncompressed/LZW TIFF or a PNG exported from the AFM software",
        )


def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _calibration_from_tiff_tags(tags: dict[int, Any]) -> Calibration:
    """TIFF XResolution/YResolution are pixels per unit; convert to pixel size."""
    x_res = tags.get(282)
    y_res = tags.get(283)
    unit_code = tags.get(296)
    description = tags.get(270)
    if isinstance(description, str) and description.strip().startswith("{"):
        try:
            payload = json.loads(description)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            px = payload.get("pixel_size_x") or payload.get("pixel_size")
            py = payload.get("pixel_size_y") or payload.get("pixel_size")
            unit = payload.get("unit")
            if px and py:
                return Calibration(
                    pixel_size_x=float(px),
                    pixel_size_y=float(py),
                    unit=str(unit) if unit else None,
                    source="image_description",
                )
    try:
        xr = float(x_res) if x_res is not None else None
        yr = float(y_res) if y_res is not None else None
    except (TypeError, ValueError):
        return Calibration()
    # ResolutionUnit 2 = inch, 3 = cm, 1 = none. A bare 1.0/1.0 with no unit
    # is the tifffile default and carries no information.
    if not xr or not yr or unit_code in (None, 1):
        return Calibration()
    unit = {2: "inch", 3: "cm"}.get(int(unit_code))
    return Calibration(
        pixel_size_x=1.0 / xr, pixel_size_y=1.0 / yr, unit=unit, source="tiff_resolution"
    )


def _describe_array(array: np.ndarray) -> tuple[int, int, int, int]:
    if array.ndim == 2:
        height, width = array.shape
        channels = 1
    elif array.ndim == 3:
        height, width, channels = array.shape
    else:
        raise ValueError(f"unsupported array shape {array.shape}")
    bit_depth = int(array.dtype.itemsize * 8)
    if array.dtype == np.bool_:
        bit_depth = 1
    return int(width), int(height), int(channels), bit_depth


def _decode_with_pillow(path: Path) -> DecodedImage:
    with Image.open(path) as opened:
        image: Image.Image = opened
        image_format = (opened.format or path.suffix.lstrip(".")).lower()
        metadata: dict[str, Any] = {"mode": image.mode}
        calibration = Calibration()
        if image_format in ("tiff", "tif"):
            tags = {int(k): v for k, v in getattr(opened, "tag_v2", {}).items()}
            compression = tags.get(259)
            metadata["compression"] = _TIFF_COMPRESSION_NAMES.get(
                int(compression) if compression is not None else -1, str(compression)
            )
            calibration = _calibration_from_tiff_tags(tags)
            frames = int(getattr(opened, "n_frames", 1))
            if frames > 1:
                metadata["frames"] = frames
        if image.mode in ("P", "PA"):
            image = image.convert("RGB")
        elif image.mode == "RGBA":
            image = image.convert("RGB")
        elif image.mode == "LA":
            image = image.convert("L")
        elif image.mode == "1":
            image = image.convert("L")
        elif image.mode in ("I;16B", "I;16L", "I;16N"):
            image = image.convert("I;16")
        array = np.asarray(image)
    if array.dtype == np.int32 and array.min() >= 0 and array.max() <= 65535:
        array = array.astype(np.uint16)
    width, height, channels, bit_depth = _describe_array(array)
    return DecodedImage(
        array=array,
        width=width,
        height=height,
        dtype=str(array.dtype),
        bit_depth=bit_depth,
        channels=channels,
        format=image_format,
        lossy_format=image_format in ("jpeg", "jpg"),
        calibration=calibration,
        metadata=metadata,
    )


def _decode_with_tifffile(path: Path) -> DecodedImage:
    import tifffile

    with tifffile.TiffFile(path) as tif:
        page = cast(Any, tif.pages[0])
        array = page.asarray()
        tags = {tag.code: tag.value for tag in page.tags.values()}
    if array.ndim == 3 and array.shape[0] in (1, 3, 4) and array.shape[2] not in (1, 3, 4):
        array = np.moveaxis(array, 0, -1)
    if array.ndim == 3 and array.shape[2] == 1:
        array = array[:, :, 0]
    width, height, channels, bit_depth = _describe_array(array)
    return DecodedImage(
        array=array,
        width=width,
        height=height,
        dtype=str(array.dtype),
        bit_depth=bit_depth,
        channels=channels,
        format="tiff",
        lossy_format=False,
        calibration=_calibration_from_tiff_tags(tags),
        metadata={"decoder": "tifffile"},
    )


def decode_image(path: Path, *, display_name: str | None = None) -> DecodedImage:
    name = display_name or path.name
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise ImageDecodeError(name, f"unsupported file extension {suffix!r}")
    try:
        return _decode_with_pillow(path)
    except (UnidentifiedImageError, OSError, ValueError) as pillow_error:
        if suffix in (".tif", ".tiff"):
            try:
                return _decode_with_tifffile(path)
            except Exception as tiff_error:  # noqa: BLE001 - report both decoders
                raise ImageDecodeError(
                    name, f"Pillow: {pillow_error}; tifffile: {tiff_error}"
                ) from tiff_error
        raise ImageDecodeError(name, str(pillow_error)) from pillow_error


def calibrations_compatible(a: Calibration, b: Calibration, tolerance: float = 1e-6) -> bool:
    """Compatible when either side has no calibration, or both agree."""
    if a.pixel_size_x is None or b.pixel_size_x is None:
        return True
    if (a.unit or None) != (b.unit or None):
        return False
    for left, right in ((a.pixel_size_x, b.pixel_size_x), (a.pixel_size_y, b.pixel_size_y)):
        if left is None or right is None:
            continue
        scale = max(abs(left), abs(right), 1e-30)
        if abs(left - right) / scale > tolerance:
            return False
    return True
