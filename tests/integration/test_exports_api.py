# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from tests.fixtures.nanounet_loader import (
    build_file_triplets,
    extract_id,
    instance_to_targets,
    load_input_image,
    load_instance_mask,
)
from tests.integration.test_regions_api import block, rows, setup_dataset


def _prepare_valid_dataset(api_client: TestClient, tmp_path: Path) -> tuple[str, str, dict]:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    a, b = classes["Layer A"], classes["Layer B"]
    api_client.post(
        f"{base}/regions",
        json={"expected_revision": 1, "class_id": a, "mask": rows(block(64, 64, 0, 64, 0, 64))},
    )
    api_client.post(
        f"{base}/regions",
        json={"expected_revision": 2, "class_id": b, "mask": rows(block(64, 64, 10, 30, 10, 30))},
    )
    api_client.post(
        f"{base}/regions",
        json={"expected_revision": 3, "class_id": b, "mask": rows(block(64, 64, 40, 50, 40, 50))},
    )
    overlaps = api_client.get(f"{base}/overlaps").json()
    for group in overlaps["groups"]:
        revision = api_client.get(base).json()["dataset"]["revision"]
        api_client.post(
            f"{base}/overlaps/{group['group_id']}/review",
            json={"expected_revision": revision, "state": "reviewed_intentional"},
        )
    return pid, dsid, classes


def test_export_is_blocked_until_validated_then_packages(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, dsid, classes = _prepare_valid_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    profiles = {
        p["profile_id"] for p in api_client.get(f"/api/v1/projects/{pid}/exports/profiles").json()
    }
    assert profiles == {"native_exact", "nanounet"}

    blocked = api_client.post(
        f"/api/v1/projects/{pid}/exports", json={"profile_id": "native_exact"}
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "VALIDATION_STALE"
    assert blocked.json()["error"]["details"]["skipped"][0]["code"] == "VALIDATION_MISSING"
    bad_profile = api_client.post(f"/api/v1/projects/{pid}/exports", json={"profile_id": "nope"})
    assert (
        bad_profile.status_code == 422
        and bad_profile.json()["error"]["code"] == "EXPORT_PROFILE_INVALID"
    )

    valid = api_client.post(f"{base}/validate").json()
    assert valid["status"] == "valid"

    created = api_client.post(
        f"/api/v1/projects/{pid}/exports",
        json={"profile_id": "native_exact", "options": {"mask_semantics": "class"}},
    )
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["profile_id"] == "native_exact" and len(record["datasets"]) == 1
    entry = record["datasets"][0]
    assert entry["composition_sha256"] == valid["composition_sha256"]
    assert entry["overlap_review_summary"] == {"reviewed_intentional": 2}
    assert entry["provenance_summary"]["region_sources"] == {"manual": 3}
    assert record["package_sha256"] and record["skipped"] == []

    download = api_client.get(f"/api/v1/projects/{pid}/exports/{record['export_id']}/download")
    assert download.status_code == 200 and download.headers["content-type"] == "application/zip"
    archive = zipfile.ZipFile(io.BytesIO(download.content))
    names = set(archive.namelist())
    assert {"manifest.json", "classes.json"} <= names
    mask_name = entry["files"]["final_mask"]
    mask = np.array(Image.open(io.BytesIO(archive.read(mask_name))))
    assert mask.dtype == np.uint16 and mask.shape == (64, 64)
    assert set(np.unique(mask).tolist()) == {classes["Layer A"], classes["Layer B"]}
    assert (mask != 0).all()
    instance = np.array(Image.open(io.BytesIO(archive.read(entry["files"]["instance_mask"]))))
    assert set(np.unique(instance).tolist()) == {1, 2, 3}
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["datasets"][0]["topography_sha256"] == entry["topography_sha256"]
    assert manifest["instances"][dsid][0]["region_id"] == "reg_000001"
    assert entry["files"]["regions"] in names and entry["files"]["topography_source"] in names

    # Determinism: a second export of the same state yields identical masks.
    again = api_client.post(
        f"/api/v1/projects/{pid}/exports", json={"profile_id": "native_exact"}
    ).json()
    archive2 = zipfile.ZipFile(
        io.BytesIO(
            api_client.get(f"/api/v1/projects/{pid}/exports/{again['export_id']}/download").content
        )
    )
    assert archive2.read(mask_name) == archive.read(mask_name)

    # Editing makes the validation stale and blocks export again.
    revision = api_client.get(base).json()["dataset"]["revision"]
    api_client.patch(
        f"{base}/regions/reg_000003", json={"expected_revision": revision, "name": "renamed"}
    )
    stale = api_client.post(f"/api/v1/projects/{pid}/exports", json={"profile_id": "native_exact"})
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["skipped"][0]["code"] == "VALIDATION_STALE"

    listing = api_client.get(f"/api/v1/projects/{pid}/exports").json()
    assert [e["export_id"] for e in listing] == [again["export_id"], record["export_id"]]
    history = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["export_history"]
    assert history[-1]["kind"] == "export"
    assert (
        api_client.delete(f"/api/v1/projects/{pid}/exports/{record['export_id']}").status_code
        == 204
    )
    assert (
        api_client.get(f"/api/v1/projects/{pid}/exports/{record['export_id']}").status_code == 404
    )


def test_nanounet_export_passes_the_downstream_loader_contract(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, dsid, classes = _prepare_valid_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    project_revision = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    boundary = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={
            "expected_revision": project_revision,
            "name": "Edge",
            "semantic_role": "Boundary",
            "export_id": 50,
        },
    ).json()
    boundary_id = boundary["classes"]["classes"][-1]["class_id"]
    api_client.patch(
        f"/api/v1/projects/{pid}",
        json={
            "expected_revision": boundary["project"]["revision"],
            "config": {"boundary_mode": "explicit_class"},
        },
    )
    revision = api_client.get(base).json()["dataset"]["revision"]
    api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": revision,
            "class_id": boundary_id,
            "mask": rows(block(64, 64, 60, 64, 0, 64)),
        },
    )
    for group in api_client.get(f"{base}/overlaps").json()["groups"]:
        if group["review_state"] == "unreviewed":
            revision = api_client.get(base).json()["dataset"]["revision"]
            api_client.post(
                f"{base}/overlaps/{group['group_id']}/review",
                json={"expected_revision": revision, "state": "reviewed_resolved"},
            )
    assert api_client.post(f"{base}/validate").json()["status"] == "valid"

    for semantics, policy in (("class", "class"), ("instance", "zero")):
        created = api_client.post(
            f"/api/v1/projects/{pid}/exports",
            json={
                "profile_id": "nanounet",
                "options": {"mask_semantics": semantics, "boundary_policy": policy},
            },
        )
        assert created.status_code == 201, created.text
        record = created.json()
        entry = record["datasets"][0]
        assert entry["export_stem"] == "700"  # digits of the pairing key
        archive = zipfile.ZipFile(
            io.BytesIO(
                api_client.get(
                    f"/api/v1/projects/{pid}/exports/{record['export_id']}/download"
                ).content
            )
        )
        out = tmp_path / f"unpacked_{semantics}"
        archive.extractall(out)
        assert (out / "Topography" / "700.png").exists()
        assert (out / "Friction" / "700.png").exists()
        assert (out / "Masks_instance" / "700.tif").exists()

        # Run the faithful copy of the downstream loader.
        triplets = build_file_triplets(
            str(out / "Topography"), str(out / "Friction"), str(out / "Masks_instance")
        )
        assert len(triplets) == 1 and extract_id(triplets[0][2]) == "700"
        img4 = load_input_image(triplets[0][0], triplets[0][1])
        assert img4.shape == (64, 64, 4) and 0.0 <= img4.min() and img4.max() <= 1.0
        gt = load_instance_mask(triplets[0][2])
        assert gt.dtype == np.int32 and gt.shape == (64, 64)
        interior, boundary_map = instance_to_targets(gt)
        assert interior.shape == (64, 64) and boundary_map.sum() > 0
        values = set(np.unique(gt).tolist())
        if semantics == "class":
            assert values == {classes["Layer A"], classes["Layer B"], 50}
            assert (gt != 0).all()
        else:
            # instance ids per winning region; Boundary rows became 0 (ignored downstream)
            assert 0 in values and gt[62, 5] == 0 and gt[5, 5] > 0
            assert len(values - {0}) == 3
        manifest = json.loads((out / "manifest.json").read_text())
        assert (
            manifest["profile"] == "nanounet" and manifest["options"]["mask_semantics"] == semantics
        )
        assert manifest["contract"] == "docs/user-guide.md#profiles"
