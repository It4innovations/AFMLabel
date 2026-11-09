# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import pytest

from afmlabel.imports.pairing import BatchFile, build_preview, extract_pairing_key


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("242_topography2.tiff", "242_2"),
        ("242_friction2.tiff", "242_2"),
        ("239_topography.tiff", "239"),
        ("239_friction.tiff", "239"),
        ("sample-A_height.png", "sample_a"),
        ("sample-A_lateral.png", "sample_a"),
        ("Topo_2024-05-01_scan07.tif", "2024_05_01_scan07"),
        ("friction.tif", ""),
        ("topography.tif", ""),
    ],
)
def test_extract_pairing_key(filename: str, expected: str) -> None:
    assert extract_pairing_key(filename) == expected


def _file(
    file_id: str, modality: str, name: str, w: int = 64, h: int = 64, error: str | None = None
) -> BatchFile:
    return BatchFile(
        file_id=file_id,
        channel=modality,
        original_filename=name,
        pairing_key=extract_pairing_key(name),
        width=None if error else w,
        height=None if error else h,
        error=error,
    )


def test_preview_pairs_by_key_and_flags_problems() -> None:
    files = [
        _file("t1", "topography", "239_topography.tiff"),
        _file("f1", "friction", "239_friction.tiff"),
        _file("t2", "topography", "240_topography.tiff"),
        _file("t3", "topography", "241_topography.tiff", w=128),
        _file("f3", "friction", "241_friction.tiff"),
        _file("f4", "friction", "242_friction.tiff", error="cannot decode"),
        _file("t4", "topography", "242_topography.tiff"),
        _file("t5", "topography", "243_topography.tiff"),
        _file("t5b", "topography", "243_topo.tiff"),
        _file("f5", "friction", "243_friction.tiff"),
    ]
    preview = build_preview(files)
    by_key = {row.pairing_key: row for row in preview.rows}
    assert by_key["239"].status == "ok"
    assert by_key["240"].status == "missing"
    assert by_key["240"].missing_channels == ["friction"]
    assert by_key["241"].status == "dimension_mismatch"
    assert by_key["242"].status == "invalid"
    assert by_key["243"].status == "ambiguous"
    assert len(by_key["243"].candidates) == 3
    assert preview.ok_count == 1 and preview.problem_count == 4
    assert preview.method == "key"
    assert not preview.order_based_fallback_available
    # Natural ordering of numeric keys.
    assert [r.pairing_key for r in preview.rows] == ["239", "240", "241", "242", "243"]


def test_order_based_fallback_requires_explicit_opt_in() -> None:
    files = [
        _file("t1", "topography", "topography.tif"),
        _file("f1", "friction", "friction.tif"),
    ]
    preview = build_preview(files)
    assert preview.method == "key"
    assert preview.order_based_fallback_available
    assert preview.rows[0].status == "ambiguous"  # both share the empty key

    confirmed = build_preview(files, allow_order_based=True)
    assert confirmed.method == "order"
    assert confirmed.rows[0].status == "ok"
    assert confirmed.rows[0].pairing_key == "pair_001"


def test_order_based_is_not_used_when_any_key_exists() -> None:
    files = [
        _file("t1", "topography", "239_topography.tif"),
        _file("f1", "friction", "friction.tif"),
    ]
    preview = build_preview(files, allow_order_based=True)
    assert preview.method == "key"
    assert {r.status for r in preview.rows} == {"missing", "ambiguous"}


def test_preview_with_three_named_channels() -> None:
    from afmlabel.domain.project import ChannelDefinition

    channels = [
        ChannelDefinition(key="topography", label="Topography", filename_tokens=["topography"]),
        ChannelDefinition(key="friction", label="Friction", filename_tokens=["friction"]),
        ChannelDefinition(key="phase", label="Phase", filename_tokens=["phase", "ph"]),
    ]
    from afmlabel.imports.pairing import channel_tokens

    tokens = channel_tokens(channels)
    assert extract_pairing_key("239_ph.tiff", tokens=tokens) == "239"

    def f(file_id: str, channel: str, name: str) -> BatchFile:
        return BatchFile(
            file_id=file_id,
            channel=channel,
            original_filename=name,
            pairing_key=extract_pairing_key(name, tokens=tokens),
            width=64,
            height=64,
        )

    files = [
        f("t1", "topography", "239_topography.tiff"),
        f("f1", "friction", "239_friction.tiff"),
        f("p1", "phase", "239_phase.tiff"),
        f("t2", "topography", "240_topography.tiff"),
        f("f2", "friction", "240_friction.tiff"),
    ]
    preview = build_preview(files, channels=channels)
    assert preview.channel_keys == ["topography", "friction", "phase"]
    by_key = {row.pairing_key: row for row in preview.rows}
    assert by_key["239"].status == "ok"
    assert by_key["239"].file_ids() == {"topography": "t1", "friction": "f1", "phase": "p1"}
    assert by_key["240"].status == "missing"
    assert by_key["240"].missing_channels == ["phase"]
    assert by_key["240"].message == "missing Phase"
