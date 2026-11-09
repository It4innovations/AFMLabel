# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Synthetic CVAT-for-images-1.1 XML builders mirroring the TSWAT export.

The regression fixture contains a large region and a
smaller region entirely inside it, listed *after* the large one so a naive
flattening pass would hide the smaller one.
"""

from __future__ import annotations

import numpy as np

_LABEL_TEMPLATE = (
    "        <label>\n"
    "          <name>{name}</name>\n"
    "          <color>{color}</color>\n"
    "          <type>any</type>\n"
    "          <attributes>\n"
    "          </attributes>\n"
    "        </label>\n"
)

_HEADER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    "<annotations>\n"
    "  <version>1.1</version>\n"
    "  <meta>\n"
    "    <job>\n"
    "      <id>5</id>\n"
    "      <mode>annotation</mode>\n"
    "      <labels>\n"
)

_FOOTER_META = (
    "      </labels>\n"
    "    </job>\n"
    "    <dumped>2026-06-05 07:41:34.391111+00:00</dumped>\n"
    "  </meta>\n"
)


def cvat_rle(mask: np.ndarray) -> tuple[str, int, int, int, int]:
    """Encode a full-image boolean mask as CVAT ``rle`` inside its bbox."""
    rows = np.flatnonzero(mask.any(axis=1))
    cols = np.flatnonzero(mask.any(axis=0))
    top, left = int(rows[0]), int(cols[0])
    height, width = int(rows[-1] - top + 1), int(cols[-1] - left + 1)
    local = mask[top : top + height, left : left + width].ravel()
    counts: list[int] = []
    value = False
    run = 0
    for pixel in local:
        if bool(pixel) == value:
            run += 1
        else:
            counts.append(run)
            value = bool(pixel)
            run = 1
    counts.append(run)
    return ", ".join(str(c) for c in counts), left, top, width, height


def mask_element(
    label: str, mask: np.ndarray, *, source: str = "semi-auto", z_order: int = 0
) -> str:
    rle, left, top, width, height = cvat_rle(mask)
    return (
        f'    <mask label="{label}" source="{source}" occluded="0" rle="{rle}" '
        f'left="{left}" top="{top}" width="{width}" height="{height}" '
        f'z_order="{z_order}">\n    </mask>\n'
    )


def polygon_element(label: str, points: list[tuple[float, float]], *, z_order: int = 0) -> str:
    text = ";".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return (
        f'    <polygon label="{label}" source="manual" occluded="0" points="{text}" '
        f'z_order="{z_order}">\n    </polygon>\n'
    )


def cvat_document(labels: dict[str, str], images: list[tuple[str, int, int, str]]) -> str:
    label_xml = "".join(
        _LABEL_TEMPLATE.format(name=name, color=color) for name, color in labels.items()
    )
    image_xml = ""
    for index, (name, width, height, body) in enumerate(images):
        image_xml += (
            f'  <image id="{index}" name="{name}" width="{width}" height="{height}">\n'
            f"{body}  </image>\n"
        )
    return _HEADER + label_xml + _FOOTER_META + image_xml + "</annotations>\n"


def hidden_region_fixture(
    width: int = 64, height: int = 64, image_name: str = "239_topography.tiff"
) -> tuple[str, dict[str, np.ndarray]]:
    """Large region, then a smaller region fully inside it (historically hidden)."""
    large = np.zeros((height, width), dtype=bool)
    large[8:56, 8:56] = True
    small = np.zeros((height, width), dtype=bool)
    small[20:30, 20:30] = True
    separate = np.zeros((height, width), dtype=bool)
    separate[0:6, 58:64] = True
    body = (
        mask_element("SLG", large)
        + mask_element("buffer", small)
        + polygon_element("BLG", [(58, 0), (63, 0), (63, 5), (58, 5)])
    )
    document = cvat_document(
        {"SLG": "#fa3253", "buffer": "#83e070", "BLG": "#fafa37"},
        [(image_name, width, height, body)],
    )
    return document, {"large": large, "small": small, "separate": separate}
