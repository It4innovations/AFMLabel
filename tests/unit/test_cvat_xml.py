# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import numpy as np
import pytest

from afmlabel.imports.cvat_xml import CvatXmlError, decode_cvat_rle, parse_cvat_xml
from tests.conftest import DEVELOPER_DATA_DIR
from tests.fixtures.cvat_xml import cvat_document, cvat_rle, hidden_region_fixture, mask_element


def test_rle_round_trip_with_bbox() -> None:
    mask = np.zeros((10, 12), dtype=bool)
    mask[2:5, 3:9] = True
    mask[3, 5] = False
    rle, left, top, width, height = cvat_rle(mask)
    assert (left, top, width, height) == (3, 2, 6, 3)
    decoded = decode_cvat_rle(rle, width, height)
    assert np.array_equal(decoded, mask[top : top + height, left : left + width])
    with pytest.raises(CvatXmlError):
        decode_cvat_rle("1, 2", 4, 4)


def test_hidden_smaller_region_survives_parsing() -> None:
    document, masks = hidden_region_fixture()
    parsed = parse_cvat_xml(document.encode("utf-8"))
    assert parsed.version == "1.1"
    assert [label.name for label in parsed.labels] == ["SLG", "buffer", "BLG"]
    assert parsed.labels[0].color == "#fa3253"
    image = parsed.images[0]
    assert image.basename == "239_topography.tiff" and (image.width, image.height) == (64, 64)
    kinds = [(s.kind, s.label, s.index) for s in image.shapes]
    assert kinds == [("mask", "SLG", 0), ("mask", "buffer", 1), ("polygon", "BLG", 2)]
    assert np.array_equal(image.shapes[0].mask, masks["large"])
    assert np.array_equal(image.shapes[1].mask, masks["small"])
    # Both masks are individually present even though the small one lies
    # entirely inside the large one.
    assert (image.shapes[0].mask & image.shapes[1].mask).sum() == masks["small"].sum()
    assert image.shapes[2].points[0] == (58.0, 0.0)
    assert image.shapes[0].source == "semi-auto" and image.shapes[2].source == "manual"


def test_out_of_bounds_shapes_are_clipped_and_unknown_tags_skipped() -> None:
    mask = np.zeros((8, 8), dtype=bool)
    mask[6:8, 6:8] = True
    element = (
        mask_element("SLG", mask).replace('left="6"', 'left="7"').replace('width="2"', 'width="2"')
    )
    body = element + '    <polyline label="x" points="1,1;2,2" z_order="0">\n    </polyline>\n'
    body += (
        '    <polygon label="BLG" source="manual" occluded="0" points="-2,-2;10,-2;10,10" '
        'z_order="3">\n    </polygon>\n'
    )
    body += '    <box label="SLG" xtl="1" ytl="1" xbr="3" ybr="3" z_order="0">\n    </box>\n'
    document = cvat_document({"SLG": "#fa3253"}, [("img.tiff", 8, 8, body)])
    parsed = parse_cvat_xml(document.encode("utf-8"))
    image = parsed.images[0]
    assert len(image.skipped) == 1 and "polyline" in image.skipped[0]
    assert [s.kind for s in image.shapes] == ["mask", "polygon", "box"]
    assert image.shapes[0].warnings and "clipped" in image.shapes[0].warnings[0]
    assert image.shapes[1].warnings and image.shapes[1].z_order == 3
    assert image.shapes[2].mask is not None and image.shapes[2].mask.sum() == 4
    assert "BLG" in parsed.label_names()  # label used without a <label> entry


def test_malformed_documents_are_rejected() -> None:
    with pytest.raises(CvatXmlError):
        parse_cvat_xml(b"<annotations><image name='x' width='a' height='2'/></annotations>")
    with pytest.raises(CvatXmlError):
        parse_cvat_xml(b"<not-annotations/>")
    with pytest.raises(CvatXmlError):
        parse_cvat_xml(b"<annotations")


def test_real_tswat_export_when_available() -> None:
    path = DEVELOPER_DATA_DIR / "data" / "annotations.xml"
    if not path.exists():
        pytest.skip("developer-local annotations.xml not present")
    parsed = parse_cvat_xml(path.read_bytes())
    assert len(parsed.images) == 18
    annotated = [img for img in parsed.images if img.shapes]
    assert len(annotated) == 9 and all(
        img.basename.endswith("_topography2.tiff") for img in annotated
    )
    total = sum(len(img.shapes) for img in annotated)
    assert total == 135  # 110 masks + 25 polygons
    first = annotated[0]
    assert (first.width, first.height) == (514, 514)
    masks = [s for s in first.shapes if s.kind == "mask"]
    assert all(s.mask is not None and s.mask.shape == (514, 514) and s.mask.any() for s in masks)
    assert not any(s.warnings for s in first.shapes)
    # Real overlaps exist in the export: at least one pair of masks intersects.
    stack = np.stack([s.mask for s in masks])
    assert (stack.sum(axis=0) > 1).any()
