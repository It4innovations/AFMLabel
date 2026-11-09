# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Parser for the legacy TSWAT annotation export: CVAT "for images 1.1" XML.

The format observed in the supplied data::

    <annotations>
      <version>1.1</version>
      <meta><job>...<labels><label><name>SLG</name><color>#fa3253</color>...
      <image id="1" name="242_topography2.tiff" width="514" height="514">
        <mask label="SLG 3" source="semi-auto" occluded="0"
              rle="182, 14, 4, ..." left="33" top="2" width="480" height="98" z_order="0"/>
        <polygon label="SLG 2" source="manual" points="30.05,5.95;45.63,28.15;..." z_order="0"/>
      </image>

* ``mask.rle`` is a comma-separated list of run lengths alternating
  background/foreground, starting with background, row-major inside the
  ``left/top/width/height`` box.
* Every shape stays an individual object; the parser never merges or
  flattens anything, so overlaps survive exactly as exported.
* Document order and ``z_order`` are preserved as provenance.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Literal
from xml.etree import ElementTree as ET

import numpy as np

IMPORTER_VERSION = "cvat-xml-1.1/afmlabel-1"

ShapeKind = Literal["mask", "polygon", "box"]
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


class CvatXmlError(ValueError):
    pass


@dataclass
class ParsedLabel:
    name: str
    color: str | None


@dataclass
class ParsedShape:
    kind: ShapeKind
    label: str
    index: int  # document order within the image
    z_order: int
    source: str | None
    occluded: bool
    #: Full-image boolean mask (height, width) for masks/boxes; polygons keep points.
    mask: np.ndarray | None = None
    points: list[tuple[float, float]] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParsedImage:
    image_id: str
    name: str
    basename: str
    width: int
    height: int
    shapes: list[ParsedShape] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


@dataclass
class ParsedAnnotations:
    version: str | None
    labels: list[ParsedLabel]
    images: list[ParsedImage]
    sha256: str
    importer_version: str = IMPORTER_VERSION

    def label_names(self) -> list[str]:
        names: list[str] = [label.name for label in self.labels]
        for image in self.images:
            for shape in image.shapes:
                if shape.label not in names:
                    names.append(shape.label)
        return names


def decode_cvat_rle(rle: str, width: int, height: int) -> np.ndarray:
    counts = [int(token) for token in rle.replace(" ", "").split(",") if token != ""]
    total = width * height
    if any(c < 0 for c in counts):
        raise CvatXmlError("negative run length in mask rle")
    if sum(counts) != total:
        raise CvatXmlError(f"mask rle sums to {sum(counts)}, expected {total} ({width}x{height})")
    flat = np.zeros(total, dtype=bool)
    position = 0
    value = False
    for run in counts:
        if value and run:
            flat[position : position + run] = True
        position += run
        value = not value
    return flat.reshape(height, width)


def _parse_points(text: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for pair in text.strip().split(";"):
        if not pair.strip():
            continue
        x_text, y_text = pair.split(",")
        points.append((float(x_text), float(y_text)))
    return points


def _int(value: str | None, name: str) -> int:
    if value is None:
        raise CvatXmlError(f"missing attribute {name}")
    try:
        return int(round(float(value)))
    except ValueError as exc:
        raise CvatXmlError(f"attribute {name} is not numeric: {value!r}") from exc


def _mask_from_element(
    element: ET.Element, width: int, height: int
) -> tuple[np.ndarray, list[str]]:
    left = _int(element.get("left"), "left")
    top = _int(element.get("top"), "top")
    box_w = _int(element.get("width"), "width")
    box_h = _int(element.get("height"), "height")
    local = decode_cvat_rle(element.get("rle", ""), box_w, box_h)
    full = np.zeros((height, width), dtype=bool)
    warnings: list[str] = []
    x0, y0 = max(left, 0), max(top, 0)
    x1, y1 = min(left + box_w, width), min(top + box_h, height)
    if (x0, y0, x1, y1) != (left, top, left + box_w, top + box_h):
        warnings.append(
            f"mask box ({left},{top},{box_w}x{box_h}) exceeds the image ({width}x{height}); clipped"
        )
    if x1 > x0 and y1 > y0:
        full[y0:y1, x0:x1] = local[y0 - top : y1 - top, x0 - left : x1 - left]
    return full, warnings


def _box_mask(element: ET.Element, width: int, height: int) -> tuple[np.ndarray, list[str]]:
    xtl, ytl = float(element.get("xtl", "nan")), float(element.get("ytl", "nan"))
    xbr, ybr = float(element.get("xbr", "nan")), float(element.get("ybr", "nan"))
    if any(np.isnan(v) for v in (xtl, ytl, xbr, ybr)):
        raise CvatXmlError("box without xtl/ytl/xbr/ybr")
    mask = np.zeros((height, width), dtype=bool)
    x0, y0 = max(0, int(round(xtl))), max(0, int(round(ytl)))
    x1, y1 = min(width, int(round(xbr))), min(height, int(round(ybr)))
    mask[y0:y1, x0:x1] = True
    warnings = []
    if xtl < 0 or ytl < 0 or xbr > width or ybr > height:
        warnings.append("box exceeds the image; clipped")
    return mask, warnings


def parse_cvat_xml(data: bytes) -> ParsedAnnotations:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise CvatXmlError(f"not well-formed XML: {exc}") from exc
    if root.tag != "annotations":
        raise CvatXmlError(f"expected <annotations> root, got <{root.tag}>")
    version = root.findtext("version")
    labels: list[ParsedLabel] = []
    for label_element in root.iter("label"):
        name = (label_element.findtext("name") or "").strip()
        if not name:
            continue
        color = (label_element.findtext("color") or "").strip()
        labels.append(ParsedLabel(name=name, color=color.lower() if _HEX.match(color) else None))

    images: list[ParsedImage] = []
    for image in root.findall("image"):
        name = image.get("name", "")
        width = _int(image.get("width"), "width")
        height = _int(image.get("height"), "height")
        parsed = ParsedImage(
            image_id=image.get("id", ""),
            name=name,
            basename=PurePath(name.replace("\\", "/")).name,
            width=width,
            height=height,
        )
        for index, element in enumerate(list(image)):
            label = (element.get("label") or "").strip()
            kind = element.tag
            if kind not in ("mask", "polygon", "box"):
                parsed.skipped.append(
                    f"{kind} #{index} ({label or 'no label'}) is not a region shape"
                )
                continue
            if not label:
                parsed.skipped.append(f"{kind} #{index} has no label")
                continue
            shape = ParsedShape(
                kind=kind,  # type: ignore[arg-type]
                label=label,
                index=index,
                z_order=_int(element.get("z_order", "0"), "z_order"),
                source=element.get("source"),
                occluded=element.get("occluded", "0") not in ("0", "false", "False", ""),
                attributes={
                    (attr.get("name") or ""): (attr.text or "")
                    for attr in element.findall("attribute")
                },
            )
            try:
                if kind == "mask":
                    shape.mask, shape.warnings = _mask_from_element(element, width, height)
                elif kind == "box":
                    shape.mask, shape.warnings = _box_mask(element, width, height)
                else:
                    shape.points = _parse_points(element.get("points", ""))
                    if len(shape.points) < 3:
                        parsed.skipped.append(f"polygon #{index} ({label}) has fewer than 3 points")
                        continue
                    if any(x < 0 or y < 0 or x > width or y > height for x, y in shape.points):
                        shape.warnings.append("polygon has points outside the image; clipped")
            except (CvatXmlError, ValueError) as exc:
                parsed.skipped.append(f"{kind} #{index} ({label}) could not be decoded: {exc}")
                continue
            parsed.shapes.append(shape)
        images.append(parsed)

    return ParsedAnnotations(
        version=version, labels=labels, images=images, sha256=hashlib.sha256(data).hexdigest()
    )
