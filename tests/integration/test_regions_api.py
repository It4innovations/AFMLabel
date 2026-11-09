# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from afmlabel.persistence.rle import decode_rows, encode_rows
from tests.conftest import create_project
from tests.integration.test_datasets_api import import_pairs


def rows(mask: np.ndarray) -> dict:
    return {"format": "rle_rows", "size": list(mask.shape), "counts": encode_rows(mask)}


def block(h: int, w: int, y0: int, y1: int, x0: int, x1: int) -> np.ndarray:
    mask = np.zeros((h, w), dtype=bool)
    mask[y0:y1, x0:x1] = True
    return mask


def setup_dataset(client: TestClient, tmp_path: Path) -> tuple[str, str, dict]:
    pid = create_project(client)["project_id"]
    classes = {}
    revision = 1
    for name in ("Layer A", "Layer B"):
        created = client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": revision, "name": name}
        ).json()
        revision = created["project"]["revision"]
        classes[name] = created["classes"]["classes"][-1]["class_id"]
    result = import_pairs(client, pid, tmp_path / "in", ["700"], revision=revision)
    return pid, result["datasets"][0]["dataset_id"], classes


def test_region_lifecycle(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    empty = api_client.get(f"{base}/regions").json()
    assert empty["regions"] == [] and empty["dataset_revision"] == 1

    created = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 1,
            "class_id": classes["Layer A"],
            "mask": rows(block(64, 64, 0, 32, 0, 64)),
        },
    )
    assert created.status_code == 201, created.text
    region = created.json()["region"]
    assert region["region_id"] == "reg_000001" and region["z_order"] == 1
    assert region["area"] == 32 * 64 and region["bbox_xywh"] == [0, 0, 64, 32]
    assert created.json()["dataset_revision"] == 2

    polygon = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 2,
            "class_id": classes["Layer B"],
            "mask": {"format": "polygon", "points": [[10, 10], [50, 10], [50, 50], [10, 50]]},
            "name": "square",
        },
    ).json()
    assert polygon["region"]["z_order"] == 2 and polygon["region"]["name"] == "square"
    poly_mask = decode_rows(polygon["region"]["mask"]["counts"], 64, 64)
    assert poly_mask[30, 30] and not poly_mask[5, 5]

    # Stale revision is refused.
    stale = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 1,
            "class_id": classes["Layer A"],
            "mask": rows(block(64, 64, 0, 4, 0, 4)),
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"

    # Unknown class, wrong size and empty masks are rejected.
    bad_class = api_client.post(
        f"{base}/regions",
        json={"expected_revision": 3, "class_id": 99, "mask": rows(block(64, 64, 0, 4, 0, 4))},
    )
    assert bad_class.json()["error"]["code"] == "CLASS_REFERENCE_INVALID"
    wrong_size = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 3,
            "class_id": classes["Layer A"],
            "mask": rows(block(32, 32, 0, 4, 0, 4)),
        },
    )
    assert wrong_size.status_code == 422
    empty_mask = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 3,
            "class_id": classes["Layer A"],
            "mask": rows(np.zeros((64, 64), dtype=bool)),
        },
    )
    assert empty_mask.status_code == 422

    # Mask edit, class change, visibility and lock.
    edited = api_client.patch(
        f"{base}/regions/reg_000001",
        json={"expected_revision": 3, "mask": rows(block(64, 64, 0, 16, 0, 64)), "visible": False},
    ).json()
    assert edited["region"]["area"] == 16 * 64 and edited["region"]["visible"] is False
    assert edited["region"]["revision"] == 2
    reclassed = api_client.patch(
        f"{base}/regions/reg_000001",
        json={"expected_revision": 4, "class_id": classes["Layer B"], "locked": True},
    ).json()
    assert reclassed["region"]["class_id"] == classes["Layer B"] and reclassed["region"]["locked"]
    locked_edit = api_client.patch(
        f"{base}/regions/reg_000001",
        json={"expected_revision": 5, "mask": rows(block(64, 64, 0, 8, 0, 64))},
    )
    assert locked_edit.status_code == 409 and locked_edit.json()["error"]["code"] == "REGION_LOCKED"
    locked_delete = api_client.delete(f"{base}/regions/reg_000001?expected_revision=5")
    assert locked_delete.json()["error"]["code"] == "REGION_LOCKED"
    unlocked = api_client.patch(
        f"{base}/regions/reg_000001", json={"expected_revision": 5, "locked": False}
    ).json()
    assert unlocked["dataset_revision"] == 6

    # Reorder: reg_000002 goes to the bottom.
    reordered = api_client.post(
        f"{base}/regions/reorder",
        json={"expected_revision": 6, "order": ["reg_000002", "reg_000001"]},
    ).json()
    assert [r["region_id"] for r in reordered["regions"]] == ["reg_000002", "reg_000001"]
    assert [r["z_order"] for r in reordered["regions"]] == [1, 2]
    bad_order = api_client.post(
        f"{base}/regions/reorder", json={"expected_revision": 7, "order": ["reg_000002"]}
    )
    assert bad_order.status_code == 422

    # Duplicate, split, merge.
    duplicated = api_client.post(
        f"{base}/regions/reg_000002/duplicate", json={"expected_revision": 7}
    ).json()
    assert duplicated["region"]["region_id"] == "reg_000003"
    two_parts = block(64, 64, 0, 8, 0, 8) | block(64, 64, 40, 48, 40, 48)
    api_client.patch(
        f"{base}/regions/reg_000003", json={"expected_revision": 8, "mask": rows(two_parts)}
    )
    split = api_client.post(
        f"{base}/regions/reg_000003/split", json={"expected_revision": 9}
    ).json()
    assert [r["region_id"] for r in split["regions"]] == ["reg_000003", "reg_000004"]
    assert split["regions"][0]["area"] == 64 and split["regions"][1]["source"] == "split"
    nothing_to_split = api_client.post(
        f"{base}/regions/reg_000004/split", json={"expected_revision": 10}
    )
    assert nothing_to_split.status_code == 422
    merged = api_client.post(
        f"{base}/regions/merge",
        json={"expected_revision": 10, "region_ids": ["reg_000003", "reg_000004"]},
    ).json()
    assert merged["region"]["area"] == 128 and merged["region"]["provenance"]["merged_from"] == [
        "reg_000004"
    ]
    api_client.patch(
        f"{base}/regions/reg_000001",
        json={"expected_revision": 11, "class_id": classes["Layer A"]},
    )
    cross_class = api_client.post(
        f"{base}/regions/merge",
        json={"expected_revision": 12, "region_ids": ["reg_000001", "reg_000002"]},
    )
    assert cross_class.json()["error"]["code"] == "CLASS_REFERENCE_INVALID"

    # Delete and audit trail.
    deleted = api_client.delete(f"{base}/regions/reg_000003?expected_revision=12").json()
    assert deleted["dataset_revision"] == 13
    listing = api_client.get(f"{base}/regions").json()
    assert [r["region_id"] for r in listing["regions"]] == ["reg_000002", "reg_000001"]
    assert listing["validated_revision"] is None
    events = [e["operation"] for e in api_client.get(f"/api/v1/projects/{pid}/events").json()]
    for op in (
        "region.create",
        "region.update",
        "regions.reorder",
        "region.split",
        "regions.merge",
        "region.delete",
    ):
        assert op in events

    # Class deletion is blocked while regions use the class.
    detail = api_client.get(f"/api/v1/projects/{pid}").json()
    class_b = classes["Layer B"]
    project_revision = detail["project"]["revision"]
    in_use = api_client.delete(
        f"/api/v1/projects/{pid}/classes/{class_b}?expected_revision={project_revision}"
    )
    assert in_use.status_code == 409 and in_use.json()["error"]["code"] == "CLASS_IN_USE"

    # Dataset summary counts regions; workspace page renders.
    summary = api_client.get(f"/api/v1/projects/{pid}/datasets").json()[0]
    assert summary["region_count"] == 2
    page = api_client.get(f"/projects/{pid}/datasets/{dsid}")
    assert page.status_code == 200 and "annotation-canvas" in page.text


def test_regions_survive_restart_and_lock_enforced(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 1,
            "class_id": classes["Layer A"],
            "mask": rows(block(64, 64, 0, 10, 0, 10)),
        },
        headers={"X-AFMLabel-Editor": "tab-a"},
    )
    api_client.post(f"/api/v1/projects/{pid}/lock", headers={"X-AFMLabel-Editor": "tab-a"})
    blocked = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 2,
            "class_id": classes["Layer A"],
            "mask": rows(block(64, 64, 0, 10, 0, 10)),
        },
        headers={"X-AFMLabel-Editor": "tab-b"},
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "PROJECT_LOCKED"
    # Reading is always allowed.
    assert (
        api_client.get(f"{base}/regions", headers={"X-AFMLabel-Editor": "tab-b"}).status_code == 200
    )
