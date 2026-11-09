# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from afmlabel.persistence.rle import decode_rows
from tests.conftest import DEVELOPER_DATA_DIR, create_project
from tests.fixtures.cvat_xml import hidden_region_fixture
from tests.fixtures.images import write_png
from tests.integration.test_datasets_api import _upload, import_pairs


def _stage(client: TestClient, pid: str, xml: str, **form: str) -> dict:
    response = client.post(
        f"/api/v1/projects/{pid}/imports/tswat/stage",
        files={"file": ("annotations.xml", xml.encode("utf-8"), "application/xml")},
        data=form,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_hidden_region_regression_import(api_client: TestClient, tmp_path: Path) -> None:
    """Both regions survive import, the overlap is reported,
    z-order can reveal the smaller one, nothing is flattened."""
    pid = create_project(api_client, "tswat")["project_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["239"], revision=1)
    dsid = result["datasets"][0]["dataset_id"]
    xml, masks = hidden_region_fixture()

    report = _stage(api_client, pid, xml)
    image = report["images"][0]
    assert image["status"] == "matched" and image["dataset_id"] == dsid
    assert image["matched_modality"] == "topography"
    assert image["mask_count"] == 2 and image["polygon_count"] == 1
    assert [label["label"] for label in report["labels"]] == ["SLG", "buffer", "BLG"]
    assert all(label["proposal"] == "create" for label in report["labels"])
    assert report["committable_dataset_count"] == 1 and report["total_shape_count"] == 3

    project_revision = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    commit = api_client.post(
        f"/api/v1/projects/{pid}/imports/tswat/{report['staged_id']}/commit",
        json={"expected_revision": project_revision, "class_mapping": {}},
    )
    assert commit.status_code == 200, commit.text
    body = commit.json()
    assert [c["name"] for c in body["created_classes"]] == ["SLG", "buffer", "BLG"]
    assert body["created_classes"][0]["color"] == "#fa3253"
    assert body["datasets"][0]["region_ids"] == ["reg_000001", "reg_000002", "reg_000003"]
    assert body["sha256"] == report["sha256"]

    regions = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/regions").json()["regions"]
    assert len(regions) == 3
    by_id = {r["region_id"]: r for r in regions}
    large, small = by_id["reg_000001"], by_id["reg_000002"]
    assert np.array_equal(decode_rows(large["mask"]["counts"], 64, 64), masks["large"])
    assert np.array_equal(decode_rows(small["mask"]["counts"], 64, 64), masks["small"])
    assert small["z_order"] > large["z_order"]  # source order preserved: later shape on top
    assert small["source"] == "tswat_import"
    assert small["provenance"]["xml_index"] == 1 and small["provenance"]["xml_label"] == "buffer"
    assert small["provenance"]["file_sha256"] == report["sha256"]
    assert small["provenance"]["importer_version"].startswith("cvat-xml-1.1")

    overlaps = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/overlaps").json()
    assert len(overlaps["groups"]) == 1
    group = overlaps["groups"][0]
    assert group["region_ids"] == ["reg_000001", "reg_000002"] and group["pixel_count"] == 100
    assert group["review_state"] == "unreviewed" and group["winning_region_id"] == "reg_000002"

    # Validation is blocked until the overlap is reviewed: no silent precedence.
    validation = api_client.post(f"/api/v1/projects/{pid}/datasets/{dsid}/validate").json()
    assert "OVERLAP_UNREVIEWED" in {i["code"] for i in validation["blocking"]}

    # Reordering hides the small one in the preview but it stays canonical.
    revision = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}").json()["dataset"][
        "revision"
    ]
    reordered = api_client.post(
        f"/api/v1/projects/{pid}/datasets/{dsid}/regions/reorder",
        json={"expected_revision": revision, "order": ["reg_000002", "reg_000001", "reg_000003"]},
    ).json()
    assert [r["region_id"] for r in reordered["regions"]] == [
        "reg_000002",
        "reg_000001",
        "reg_000003",
    ]
    overlaps = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/overlaps").json()
    assert overlaps["groups"][0]["winning_region_id"] == "reg_000001"
    assert overlaps["groups"][0]["group_id"] == group["group_id"]
    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert any(e["operation"] == "import.tswat" and e["source"] == "tswat_import" for e in events)
    history = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["import_history"]
    assert (
        history[-1]["kind"] == "import_tswat"
        and history[-1]["details"]["sha256"] == report["sha256"]
    )


def test_dimension_mismatch_and_unmatched_images_are_not_imported(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid = create_project(api_client)["project_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["239"], revision=1)
    dsid = result["datasets"][0]["dataset_id"]
    mismatch, _ = hidden_region_fixture(width=32, height=32)
    report = _stage(api_client, pid, mismatch)
    assert report["images"][0]["status"] == "dimension_mismatch"
    assert report["committable_dataset_count"] == 0
    unmatched, _ = hidden_region_fixture(image_name="unknown.tiff")
    report2 = _stage(api_client, pid, unmatched)
    assert report2["images"][0]["status"] == "unmatched"
    # An explicit target dataset resolves a single unmatched image.
    report3 = _stage(api_client, pid, unmatched, target_dataset_id=dsid)
    assert report3["images"][0]["status"] == "matched"
    assert report3["images"][0]["matched_modality"] == "explicit"
    project_revision = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    commit = api_client.post(
        f"/api/v1/projects/{pid}/imports/tswat/{report3['staged_id']}/commit",
        json={
            "expected_revision": project_revision,
            "class_mapping": {"BLG": "skip"},
            "target_dataset_id": dsid,
        },
    ).json()
    assert len(commit["datasets"][0]["region_ids"]) == 2
    assert [c["name"] for c in commit["created_classes"]] == ["SLG", "buffer"]
    assert any("skipped" in w for w in commit["datasets"][0]["warnings"])
    # Existing classes are reused on a second import instead of duplicated.
    again = _stage(api_client, pid, unmatched, target_dataset_id=dsid)
    assert {label["label"]: label["proposal"] for label in again["labels"]} == {
        "SLG": "existing",
        "buffer": "existing",
        "BLG": "create",
    }
    assert (
        api_client.delete(f"/api/v1/projects/{pid}/imports/tswat/{again['staged_id']}").status_code
        == 204
    )
    bad = api_client.post(
        f"/api/v1/projects/{pid}/imports/tswat/stage",
        files={"file": ("x.xml", b"<nope/>", "application/xml")},
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "TSWAT_IMPORT_INVALID"


def test_lossy_raster_import_is_marked_and_warned(api_client: TestClient, tmp_path: Path) -> None:
    pid = create_project(api_client)["project_id"]
    rev = 1
    created = api_client.post(
        f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": "Layer A"}
    ).json()
    rev = created["project"]["revision"]
    class_a = created["classes"]["classes"][0]["class_id"]
    created = api_client.post(
        f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": "Layer B"}
    ).json()
    rev = created["project"]["revision"]
    class_b = created["classes"]["classes"][1]["class_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["300"], revision=rev)
    dsid = result["datasets"][0]["dataset_id"]
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"

    labels = np.zeros((64, 64), dtype=np.uint8)
    labels[:32, :] = 1
    labels[32:, :32] = 2
    labels[40:48, 40:48] = 2  # second component of value 2
    raster = write_png(tmp_path / "flat.png", labels)
    with raster.open("rb") as handle:
        staged = api_client.post(
            f"{base}/imports/lossy-mask/stage", files={"file": ("flat.png", handle, "image/png")}
        ).json()
    preview = staged["preview"]
    assert preview["compatible"] and "lossy" in preview["warning"]
    assert [(v["value"], v["component_count"]) for v in preview["values"]] == [(1, 1), (2, 2)]

    commit = api_client.post(
        f"{base}/imports/lossy-mask/commit",
        json={
            "staged_id": staged["staged_id"],
            "expected_revision": 1,
            "value_to_class": {"1": class_a, "2": class_b},
        },
    )
    assert commit.status_code == 200, commit.text
    assert len(commit.json()["region_ids"]) == 3
    regions = api_client.get(f"{base}/regions").json()["regions"]
    assert all(r["source"] == "lossy_import" for r in regions)
    assert regions[0]["provenance"]["lossy_flattened_import"] is True
    validation = api_client.post(f"{base}/validate").json()
    assert "TSWAT_IMPORT_LOSSY" in {w["code"] for w in validation["warnings"]}
    assert "UNLABELED_PIXELS" in {i["code"] for i in validation["blocking"]}

    wrong_size = write_png(tmp_path / "small.png", np.ones((8, 8), dtype=np.uint8))
    with wrong_size.open("rb") as handle:
        bad = api_client.post(
            f"{base}/imports/lossy-mask/stage", files={"file": ("small.png", handle, "image/png")}
        ).json()
    assert bad["preview"]["compatible"] is False


def test_real_tswat_export_import_when_available(api_client: TestClient) -> None:
    cvat = DEVELOPER_DATA_DIR / "data"
    if not (cvat / "annotations.xml").exists():
        pytest.skip("developer-local annotations.xml not present")
    pid = create_project(api_client, "real tswat")["project_id"]
    batch_id = api_client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    _upload(api_client, pid, batch_id, "topography", [cvat / "images" / "242_topography2.tiff"])
    preview = _upload(
        api_client, pid, batch_id, "friction", [cvat / "images" / "242_friction2.tiff"]
    )
    row = preview["rows"][0]
    commit = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={
            "expected_revision": 1,
            "pairs": [
                {
                    "pairing_key": row["pairing_key"],
                    "topography_file_id": row["files"]["topography"]["file_id"],
                    "friction_file_id": row["files"]["friction"]["file_id"],
                }
            ],
        },
    ).json()
    dsid = commit["datasets"][0]["dataset_id"]
    report = _stage(api_client, pid, (cvat / "annotations.xml").read_text(encoding="utf-8"))
    matched = [img for img in report["images"] if img["status"] == "matched"]
    assert [img["image_name"] for img in matched] == ["242_topography2.tiff"]
    assert matched[0]["shape_count"] == 15
    unmatched = [img for img in report["images"] if img["status"] == "unmatched"]
    assert len(unmatched) == 8  # the other annotated topography images
    result = api_client.post(
        f"/api/v1/projects/{pid}/imports/tswat/{report['staged_id']}/commit",
        json={"expected_revision": commit["project_revision"], "class_mapping": {}},
    ).json()
    assert len(result["datasets"][0]["region_ids"]) == 15
    overlaps = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/overlaps").json()
    assert overlaps["groups"], "the real export contains overlapping shapes that must be visible"
    assert overlaps["unreviewed_count"] == len(overlaps["groups"])
    assert _ == _ or True


_ = None
