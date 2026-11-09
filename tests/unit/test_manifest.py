# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

from afmlabel.segmentation.manifest import (
    build_manifest,
    load_manifest,
    verify_manifest,
    write_manifest,
)


def test_manifest_round_trip_and_verification(tmp_path: Path) -> None:
    (tmp_path / "model.safetensors").write_bytes(b"\x00" * 32)
    (tmp_path / "config.json").write_text("{}")
    manifest = build_manifest(
        model_id="sam31", repository="facebook/sam3.1", revision="abc123", root=tmp_path
    )
    assert manifest.checkpoint_file == "model.safetensors"
    assert manifest.checkpoint_sha256 is not None
    path = write_manifest(manifest, tmp_path)
    loaded = load_manifest(path)
    assert loaded == manifest
    assert verify_manifest(loaded, tmp_path) == []

    (tmp_path / "model.safetensors").write_bytes(b"\x01" * 32)
    problems = verify_manifest(loaded, tmp_path)
    assert problems == ["sha256 mismatch: model.safetensors"]
