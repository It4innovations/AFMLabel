# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.integration.test_regions_api import block, rows, setup_dataset


def test_overlap_review_and_validation_flow(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    a, b = classes["Layer A"], classes["Layer B"]
    api_client.post(
        f"{base}/regions",
        json={"expected_revision": 1, "class_id": a, "mask": rows(block(64, 64, 0, 64, 0, 64))},
    )
    api_client.post(
        f"{base}/regions",
        json={"expected_revision": 2, "class_id": b, "mask": rows(block(64, 64, 20, 30, 20, 30))},
    )

    overlaps = api_client.get(f"{base}/overlaps").json()
    assert overlaps["coverage"]["unlabeled_pixels"] == 0
    assert len(overlaps["groups"]) == 1 and overlaps["unreviewed_count"] == 1
    group = overlaps["groups"][0]
    assert group["region_ids"] == ["reg_000001", "reg_000002"] and group["max_multiplicity"] == 2
    assert group["winning_region_id"] == "reg_000002"

    # Unreviewed overlap blocks validation.
    blocked = api_client.post(f"{base}/validate").json()
    assert blocked["status"] == "blocked"
    assert [i["code"] for i in blocked["blocking"]] == ["OVERLAP_UNREVIEWED"]
    status = api_client.get(f"{base}/validation").json()
    assert status["stale"] is False and status["report"]["status"] == "blocked"
    assert api_client.get(f"{base}/regions").json()["validated_revision"] is None

    # Review it, then validation passes and records the revision.
    reviewed = api_client.post(
        f"{base}/overlaps/{group['group_id']}/review",
        json={"expected_revision": 3, "state": "reviewed_intentional", "note": "layer B sits on A"},
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["report"]["groups"][0]["review_state"] == "reviewed_intentional"
    assert reviewed.json()["dataset_revision"] == 4
    valid = api_client.post(f"{base}/validate").json()
    assert valid["status"] == "valid" and valid["dataset_revision"] == 4
    assert [w["code"] for w in valid["warnings"]] == ["OVERLAP_MULTIPLE"]
    assert valid["class_pixel_histogram"] == {str(a): 64 * 64 - 100, str(b): 100}
    regions = api_client.get(f"{base}/regions").json()
    assert regions["validated_revision"] == 4 and regions["dataset_revision"] == 4

    # Reordering changes the winner but keeps the review (same pixels).
    api_client.post(
        f"{base}/regions/reorder",
        json={"expected_revision": 4, "order": ["reg_000002", "reg_000001"]},
    )
    after = api_client.get(f"{base}/overlaps").json()
    assert after["groups"][0]["winning_region_id"] == "reg_000001"
    assert after["groups"][0]["review_state"] == "reviewed_intentional"
    stale = api_client.get(f"{base}/validation").json()
    assert stale["stale"] is True and stale["current_revision"] == 5
    assert api_client.get(f"{base}/regions").json()["validated_revision"] is None

    # Editing the overlapping pixels invalidates the review.
    api_client.patch(
        f"{base}/regions/reg_000002",
        json={"expected_revision": 5, "mask": rows(block(64, 64, 20, 31, 20, 31))},
    )
    changed = api_client.get(f"{base}/overlaps").json()
    assert changed["groups"][0]["review_state"] == "unreviewed"
    assert changed["groups"][0]["group_id"] != group["group_id"]
    gone = api_client.post(
        f"{base}/overlaps/{group['group_id']}/review",
        json={"expected_revision": 6, "state": "reviewed_resolved"},
    )
    assert gone.status_code == 404

    # Hidden region blocks; missing pixels block.
    api_client.patch(f"{base}/regions/reg_000002", json={"expected_revision": 6, "visible": False})
    hidden = api_client.post(f"{base}/validate").json()
    assert "REGION_HIDDEN" in {i["code"] for i in hidden["blocking"]}
    api_client.patch(f"{base}/regions/reg_000002", json={"expected_revision": 7, "visible": True})
    api_client.patch(
        f"{base}/regions/reg_000001",
        json={"expected_revision": 8, "mask": rows(block(64, 64, 0, 32, 0, 64))},
    )
    missing = api_client.get(f"{base}/overlaps").json()
    assert missing["coverage"]["unlabeled_pixels"] == 32 * 64
    assert missing["missing_components"][0]["bbox_xywh"] == [0, 32, 64, 32]
    report = api_client.post(f"{base}/validate").json()
    assert "UNLABELED_PIXELS" in {i["code"] for i in report["blocking"]}
    events = [e["operation"] for e in api_client.get(f"/api/v1/projects/{pid}/events").json()]
    assert events.count("dataset.validate") == 4 and "overlap.review" in events


def test_boundary_fill_flow(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    project = api_client.get(f"/api/v1/projects/{pid}").json()
    rev = project["project"]["revision"]
    boundary = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={"expected_revision": rev, "name": "Edge", "semantic_role": "Boundary"},
    ).json()
    boundary_id = boundary["classes"]["classes"][-1]["class_id"]
    api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 1,
            "class_id": classes["Layer A"],
            "mask": rows(block(64, 64, 0, 40, 0, 64)),
        },
    )

    # Default policy (disabled): fill is refused and Boundary regions violate.
    preview = api_client.get(f"{base}/boundary-fill/preview").json()
    assert preview["allowed"] is False and preview["pixel_count"] == 24 * 64
    refused = api_client.post(
        f"{base}/boundary-fill",
        json={"expected_revision": 2, "confirmed_pixel_count": preview["pixel_count"]},
    )
    assert (
        refused.status_code == 409
        and refused.json()["error"]["code"] == "BOUNDARY_POLICY_VIOLATION"
    )

    # Switch the project policy, then confirm with the wrong count -> refused.
    api_client.patch(
        f"/api/v1/projects/{pid}",
        json={
            "expected_revision": boundary["project"]["revision"],
            "config": {"boundary_mode": "fill_unlabeled_after_approval"},
        },
    )
    preview = api_client.get(f"{base}/boundary-fill/preview").json()
    assert preview["allowed"] is True and preview["boundary_class_id"] == boundary_id
    wrong = api_client.post(
        f"{base}/boundary-fill", json={"expected_revision": 2, "confirmed_pixel_count": 1}
    )
    assert wrong.status_code == 409 and wrong.json()["error"]["details"]["pixel_count"] == 24 * 64
    before_validation = api_client.post(f"{base}/validate").json()
    assert "UNLABELED_PIXELS" in {i["code"] for i in before_validation["blocking"]}
    assert "approve a Boundary fill" in next(
        i["message"] for i in before_validation["blocking"] if i["code"] == "UNLABELED_PIXELS"
    )

    filled = api_client.post(
        f"{base}/boundary-fill",
        json={"expected_revision": 2, "confirmed_pixel_count": preview["pixel_count"]},
    )
    assert filled.status_code == 200, filled.text
    region = filled.json()["region"]
    assert region["source"] == "boundary_fill" and region["class_id"] == boundary_id
    assert region["z_order"] == 1 and region["area"] == 24 * 64
    regions = api_client.get(f"{base}/regions").json()["regions"]
    assert [r["region_id"] for r in regions] == ["reg_000002", "reg_000001"]  # fill at the bottom
    valid = api_client.post(f"{base}/validate").json()
    assert valid["status"] == "valid"
    assert valid["class_pixel_histogram"] == {
        str(classes["Layer A"]): 40 * 64,
        str(boundary_id): 24 * 64,
    }
    assert "BOUNDARY_POLICY_VIOLATION" in {w["code"] for w in valid["warnings"]}
    nothing = api_client.post(
        f"{base}/boundary-fill", json={"expected_revision": 3, "confirmed_pixel_count": 0}
    )
    assert nothing.status_code == 409 and nothing.json()["error"]["code"] == "UNLABELED_PIXELS"
