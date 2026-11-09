# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Server half of the fill tool: ``POST /regions/bulk-create`` creates several
regions in one revisioned, undoable transaction."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.integration.test_regions_api import block, rows, setup_dataset


def test_bulk_create_is_atomic_and_audited(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    items = [
        {
            "mask": rows(block(64, 64, 0, 8, 0, 8)),
            "name": "grain",
            "provenance": {"tool": "fill_all"},
        },
        {"mask": rows(block(64, 64, 20, 30, 20, 30)), "name": "grain"},
    ]
    created = api_client.post(
        f"{base}/regions/bulk-create",
        json={
            "expected_revision": 1,
            "class_id": classes["Layer A"],
            "items": items,
            "operation": "fill_gaps",
        },
    )
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["dataset_revision"] == 2
    assert [r["region_id"] for r in payload["regions"]] == ["reg_000001", "reg_000002"]
    assert [r["z_order"] for r in payload["regions"]] == [1, 2]
    assert all(
        r["name"] == "grain" and r["class_id"] == classes["Layer A"] for r in payload["regions"]
    )
    assert payload["regions"][0]["provenance"] == {"tool": "fill_all"}
    assert payload["regions"][0]["area"] == 64 and payload["regions"][1]["area"] == 100

    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert "regions.fill_gaps" in [e["operation"] for e in events]

    # One empty mask makes the whole call fail; nothing is created.
    bad = api_client.post(
        f"{base}/regions/bulk-create",
        json={
            "expected_revision": 2,
            "class_id": classes["Layer A"],
            "items": [
                {"mask": rows(block(64, 64, 40, 48, 40, 48))},
                {"mask": rows(block(64, 64, 0, 0, 0, 0))},
            ],
        },
    )
    assert bad.status_code == 422
    listing = api_client.get(f"{base}/regions").json()
    assert len(listing["regions"]) == 2 and listing["dataset_revision"] == 2

    unknown_class = api_client.post(
        f"{base}/regions/bulk-create",
        json={"expected_revision": 2, "class_id": 999, "items": [items[0]]},
    )
    assert unknown_class.status_code == 422
    assert unknown_class.json()["error"]["code"] == "CLASS_REFERENCE_INVALID"

    stale = api_client.post(
        f"{base}/regions/bulk-create",
        json={"expected_revision": 1, "class_id": classes["Layer A"], "items": [items[0]]},
    )
    assert stale.status_code == 409
