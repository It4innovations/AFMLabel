# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Integration tests for the upgrade's API surface: subtract-by-order,
bulk delete, snapshot restore, live Findings and bulk review, flattened
import rules, export options/naming/colour mask, dataset search, Help."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from tests.conftest import create_project
from tests.fixtures.images import write_png
from tests.fixtures.nanounet_loader import build_file_triplets, extract_id, load_instance_mask
from tests.integration.test_datasets_api import import_pairs
from tests.integration.test_regions_api import block, rows, setup_dataset


def _revision(client: TestClient, base: str) -> int:
    return int(client.get(base).json()["dataset"]["revision"])


def _create(client: TestClient, base: str, class_id: int, mask: np.ndarray) -> str:
    response = client.post(
        f"{base}/regions",
        json={
            "expected_revision": _revision(client, base),
            "class_id": class_id,
            "mask": rows(mask),
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["region"]["region_id"])


# ------------------------------------------------------------ regions


def test_subtract_by_order_preview_commit_and_locked_refusal(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    a = _create(api_client, base, classes["Layer A"], block(64, 64, 0, 64, 0, 64))
    b = _create(api_client, base, classes["Layer B"], block(64, 64, 10, 20, 10, 20))
    c = _create(api_client, base, classes["Layer B"], block(64, 64, 12, 18, 12, 18))
    preview = api_client.post(
        f"{base}/regions/subtract-by-order/preview", json={"region_ids": [a, b, c]}
    ).json()
    assert preview["order_top_to_bottom"] == [c, b, a]
    assert preview["total_pixels_removed"] == 36 + 100
    assert preview["empty_region_ids"] == [] and preview["locked_region_ids"] == []
    # Lock the bottom region: it would lose pixels -> refused.
    api_client.patch(
        f"{base}/regions/{a}",
        json={"expected_revision": _revision(api_client, base), "locked": True},
    )
    refused = api_client.post(
        f"{base}/regions/subtract-by-order",
        json={"expected_revision": _revision(api_client, base), "region_ids": [a, b, c]},
    )
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "REGION_LOCKED"
    assert refused.json()["error"]["details"]["locked_region_ids"] == [a]
    api_client.patch(
        f"{base}/regions/{a}",
        json={"expected_revision": _revision(api_client, base), "locked": False},
    )
    done = api_client.post(
        f"{base}/regions/subtract-by-order",
        json={"expected_revision": _revision(api_client, base), "region_ids": [a, b, c]},
    )
    assert done.status_code == 200, done.text
    areas = {r["region_id"]: r["area"] for r in done.json()["regions"]}
    assert areas == {a: 64 * 64 - 100, b: 100 - 36, c: 36}
    assert done.json()["removed_region_ids"] == []
    findings = api_client.get(f"{base}/findings").json()
    assert not [f for f in findings["findings"] if f["type"] == "overlap"]
    events = [e["operation"] for e in api_client.get(f"/api/v1/projects/{pid}/events").json()]
    assert "regions.subtract_by_order" in events


def test_subtract_by_order_empty_region_needs_confirmation(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    small = _create(api_client, base, classes["Layer A"], block(64, 64, 5, 8, 5, 8))
    cover = _create(api_client, base, classes["Layer B"], block(64, 64, 0, 64, 0, 64))
    preview = api_client.post(
        f"{base}/regions/subtract-by-order/preview", json={"region_ids": [small, cover]}
    ).json()
    assert preview["empty_region_ids"] == [small]
    blocked = api_client.post(
        f"{base}/regions/subtract-by-order",
        json={"expected_revision": _revision(api_client, base), "region_ids": [small, cover]},
    )
    assert blocked.status_code == 409 and blocked.json()["error"]["details"][
        "empty_region_ids"
    ] == [small]
    assert len(api_client.get(f"{base}/regions").json()["regions"]) == 2
    ok = api_client.post(
        f"{base}/regions/subtract-by-order",
        json={
            "expected_revision": _revision(api_client, base),
            "region_ids": [small, cover],
            "allow_empty_removal": True,
        },
    ).json()
    assert ok["removed_region_ids"] == [small]
    assert [r["region_id"] for r in ok["regions"]] == [cover]


def test_bulk_delete_and_restore_round_trip(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    a = _create(api_client, base, classes["Layer A"], block(64, 64, 0, 10, 0, 10))
    b = _create(api_client, base, classes["Layer B"], block(64, 64, 20, 30, 20, 30))
    c = _create(api_client, base, classes["Layer B"], block(64, 64, 40, 50, 40, 50))
    api_client.patch(
        f"{base}/regions/{c}",
        json={"expected_revision": _revision(api_client, base), "locked": True, "name": "keep"},
    )
    snapshot = api_client.get(f"{base}/regions").json()
    deleted = api_client.post(
        f"{base}/regions/bulk-delete",
        json={"expected_revision": snapshot["dataset_revision"], "region_ids": [a, b, c]},
    ).json()
    assert deleted["deleted_region_ids"] == [a, b] and deleted["kept_locked_region_ids"] == [c]
    assert [r["region_id"] for r in api_client.get(f"{base}/regions").json()["regions"]] == [c]

    restored = api_client.post(
        f"{base}/regions/restore",
        json={
            "expected_revision": deleted["dataset_revision"],
            "regions": snapshot["regions"],
            "overlap_reviews": snapshot["overlap_reviews"],
            "next_region_seq": snapshot["next_region_seq"],
            "reason": "undo delete",
        },
    )
    assert restored.status_code == 200, restored.text
    after = restored.json()
    assert [r["region_id"] for r in after["regions"]] == [a, b, c]
    assert [r["z_order"] for r in after["regions"]] == [1, 2, 3]
    assert after["regions"][2]["locked"] is True and after["regions"][2]["name"] == "keep"
    assert after["regions"][0]["mask"]["counts"] == snapshot["regions"][0]["mask"]["counts"]
    assert (
        after["validated_revision"] is None
        and after["dataset_revision"] == deleted["dataset_revision"] + 1
    )
    # Stale expected revision is refused like every other mutation.
    stale = api_client.post(
        f"{base}/regions/restore",
        json={"expected_revision": 1, "regions": snapshot["regions"], "reason": "x"},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "REVISION_CONFLICT"
    events = [e["operation"] for e in api_client.get(f"/api/v1/projects/{pid}/events").json()]
    assert "regions.delete_selected" in events and "regions.restore" in events


def test_merge_then_restore_returns_original_ids(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    a = _create(api_client, base, classes["Layer A"], block(64, 64, 0, 10, 0, 10))
    b = _create(api_client, base, classes["Layer A"], block(64, 64, 20, 30, 20, 30))
    snapshot = api_client.get(f"{base}/regions").json()
    merged = api_client.post(
        f"{base}/regions/merge",
        json={"expected_revision": snapshot["dataset_revision"], "region_ids": [a, b]},
    ).json()
    assert merged["region"]["region_id"] == a and merged["region"]["area"] == 200
    after = api_client.post(
        f"{base}/regions/restore",
        json={
            "expected_revision": merged["dataset_revision"],
            "regions": snapshot["regions"],
            "next_region_seq": snapshot["next_region_seq"],
            "reason": "undo merge",
        },
    ).json()
    assert [(r["region_id"], r["area"]) for r in after["regions"]] == [(a, 100), (b, 100)]
    # New regions after the restore get a fresh id, never a recycled one.
    c = _create(api_client, base, classes["Layer A"], block(64, 64, 40, 41, 40, 41))
    assert c not in (a, b)


# ----------------------------------------------------------- findings


def test_findings_are_live_sorted_and_bulk_reviewable(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    _create(api_client, base, classes["Layer A"], block(64, 64, 0, 40, 0, 64))
    _create(api_client, base, classes["Layer B"], block(64, 64, 0, 8, 0, 8))
    _create(api_client, base, classes["Layer B"], block(64, 64, 10, 18, 10, 18))
    findings = api_client.get(f"{base}/findings").json()
    kinds = [(f["type"], f["severity"], f["review_state"]) for f in findings["findings"]]
    assert kinds == [
        ("missing_coverage", "blocking", "not_applicable"),
        ("overlap", "blocking", "unreviewed"),
        ("overlap", "blocking", "unreviewed"),
    ]
    assert findings["counts"] == {
        "blocking_unreviewed": 3,
        "warning_unreviewed": 0,
        "reviewed": 0,
        "total": 3,
    }
    assert [t["type"] for t in findings["types"]] == ["missing_coverage", "overlap"]
    assert findings["stored_validation_status"] is None and findings["validation_stale"] is False
    missing = findings["findings"][0]
    assert (
        missing["requires_correction"]
        and not missing["reviewable"]
        and missing["bbox_xywh"] == [0, 40, 64, 24]
    )

    groups = [f["group_id"] for f in findings["findings"] if f["type"] == "overlap"]
    reviewed = api_client.post(
        f"{base}/findings/review",
        json={
            "expected_revision": findings["dataset_revision"],
            "group_ids": groups + ["ovl_missing"],
            "state": "reviewed_resolved",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    body = reviewed.json()
    assert body["applied_group_ids"] == groups and body["missing_group_ids"] == ["ovl_missing"]
    kinds = [(f["type"], f["severity"], f["review_state"]) for f in body["findings"]["findings"]]
    assert kinds == [
        ("missing_coverage", "blocking", "not_applicable"),
        ("overlap", "warning", "reviewed"),
        ("overlap", "warning", "reviewed"),
    ]
    # Review never changed geometry.
    regions = api_client.get(f"{base}/regions").json()["regions"]
    assert sorted(r["area"] for r in regions) == [64, 64, 40 * 64]
    # Stored validation becomes stale after a later mutation and the live list says so.
    api_client.post(f"{base}/validate")
    assert api_client.get(f"{base}/findings").json()["validation_stale"] is False
    api_client.patch(
        f"{base}/regions/reg_000002",
        json={"expected_revision": _revision(api_client, base), "name": "x"},
    )
    live = api_client.get(f"{base}/findings").json()
    assert live["validation_stale"] is True and live["stored_validation_status"] == "blocked"
    nothing = api_client.post(
        f"{base}/findings/review",
        json={
            "expected_revision": _revision(api_client, base),
            "group_ids": ["ovl_nope"],
            "state": "reviewed_resolved",
        },
    )
    assert nothing.status_code == 404


# ------------------------------------------------------------- import


def test_flattened_import_rules(api_client: TestClient, tmp_path: Path) -> None:
    pid = create_project(api_client)["project_id"]
    rev = 1
    created = api_client.post(
        f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": "SLG"}
    ).json()
    rev = created["project"]["revision"]
    slg = created["classes"]["classes"][0]["class_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["300"], revision=rev)
    dsid = result["datasets"][0]["dataset_id"]
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    labels = np.zeros((64, 64), dtype=np.uint8)
    labels[:16, :] = 1
    labels[40:48, 40:48] = 2
    raster = write_png(tmp_path / "flat.png", labels)

    def stage() -> dict:
        with raster.open("rb") as handle:
            return api_client.post(
                f"{base}/imports/lossy-mask/stage",
                files={"file": ("flat.png", handle, "image/png")},
            ).json()

    # No buffer class and no default: the user must choose.
    staged = stage()
    preview = staged["preview"]
    assert preview["requires_class_choice"] is True and preview["default_class_id"] is None
    assert all(v["suggested_class_id"] is None for v in preview["values"])
    assert preview["expected_width"] == 64
    # Missing mapping -> 422, nothing imported.
    incomplete = api_client.post(
        f"{base}/imports/lossy-mask/commit",
        json={
            "staged_id": staged["staged_id"],
            "expected_revision": 1,
            "value_to_class": {"1": slg},
        },
    )
    assert incomplete.status_code == 422 and incomplete.json()["error"]["details"][
        "unmapped_values"
    ] == [2]
    # Discard without confirmation -> 409, staged raster kept for a retry.
    unconfirmed = api_client.post(
        f"{base}/imports/lossy-mask/commit",
        json={
            "staged_id": staged["staged_id"],
            "expected_revision": 1,
            "value_to_class": {"1": slg, "2": None},
        },
    )
    assert unconfirmed.status_code == 409 and unconfirmed.json()["error"]["details"][
        "discarded_values"
    ] == [2]
    assert api_client.get(f"{base}/regions").json()["regions"] == []
    confirmed = api_client.post(
        f"{base}/imports/lossy-mask/commit",
        json={
            "staged_id": staged["staged_id"],
            "expected_revision": 1,
            "value_to_class": {"1": slg, "2": None},
            "confirm_discard": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["skipped_values"] == [2] and body["mapping_used"] == {"1": slg}
    assert body["active_class_id"] == slg and body["imported_pixels"] == 16 * 64
    assert body["bbox_xywh"] == [0, 0, 64, 16] and "Imported 1 region" in body["summary"]
    region = api_client.get(f"{base}/regions").json()["regions"][0]
    assert (
        region["provenance"]["dimensions"] == [64, 64]
        and region["provenance"]["importer_version"] == "lossy-raster/afmlabel-2"
    )
    event = next(
        e
        for e in api_client.get(f"/api/v1/projects/{pid}/events").json()
        if e["operation"] == "regions.import_lossy"
    )
    assert event["details"]["source_values"] == [1, 2] and event["details"]["mapping_used"] == {
        "1": slg
    }

    # Add a Buffer class: it becomes the proposal.
    rev = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    buffer = api_client.post(
        f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": "Buffer"}
    ).json()
    buffer_id = buffer["classes"]["classes"][-1]["class_id"]
    preview = stage()["preview"]
    assert (
        preview["default_class_id"] == buffer_id
        and preview["default_class_source"] == "buffer class"
    )
    assert all(v["suggested_class_id"] == buffer_id for v in preview["values"])
    # Configured default import class is used when no Buffer class is enabled.
    rev = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    rev = api_client.patch(
        f"/api/v1/projects/{pid}/classes/{buffer_id}",
        json={"expected_revision": rev, "enabled": False},
    ).json()["project"]["revision"]
    api_client.patch(
        f"/api/v1/projects/{pid}",
        json={"expected_revision": rev, "config": {"default_import_class_key": "slg"}},
    )
    preview = stage()["preview"]
    assert (
        preview["default_class_id"] == slg
        and preview["default_class_source"] == "project default import class"
    )

    # Dimension mismatch blocks with the exact message.
    wrong = write_png(tmp_path / "small.png", np.ones((8, 8), dtype=np.uint8))
    with wrong.open("rb") as handle:
        bad = api_client.post(
            f"{base}/imports/lossy-mask/stage", files={"file": ("small.png", handle, "image/png")}
        ).json()
    assert bad["preview"]["compatible"] is False
    assert (
        "Expected: 64 x 64" in bad["preview"]["reason"]
        and "Received: 8 x 8" in bad["preview"]["reason"]
    )


# -------------------------------------------------------------- export


def _prepare_valid(
    api_client: TestClient, tmp_path: Path, keys: list[str]
) -> tuple[str, list[str], dict]:
    pid = create_project(api_client)["project_id"]
    created = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={"expected_revision": 1, "name": "Layer A", "display_color": "#fa3253"},
    ).json()
    rev = created["project"]["revision"]
    created = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={
            "expected_revision": rev,
            "name": "Layer B",
            "display_color": "#00a398",
            "export_id": 9,
        },
    ).json()
    rev = created["project"]["revision"]
    classes = {c["name"]: c for c in created["classes"]["classes"]}
    result = import_pairs(api_client, pid, tmp_path / "in", keys, revision=rev)
    ids = []
    for entry in result["datasets"]:
        dsid = entry["dataset_id"]
        base = f"/api/v1/projects/{pid}/datasets/{dsid}"
        _create(api_client, base, classes["Layer A"]["class_id"], block(64, 64, 0, 64, 0, 64))
        _create(api_client, base, classes["Layer B"]["class_id"], block(64, 64, 10, 20, 10, 20))
        for group in api_client.get(f"{base}/overlaps").json()["groups"]:
            api_client.post(
                f"{base}/overlaps/{group['group_id']}/review",
                json={
                    "expected_revision": _revision(api_client, base),
                    "state": "reviewed_resolved",
                },
            )
        assert api_client.post(f"{base}/validate").json()["status"] == "valid"
        ids.append(dsid)
    return pid, ids, classes


def test_export_default_profile_naming_png_tiff_and_color_mask(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, ids, classes = _prepare_valid(api_client, tmp_path, ["242_2", "242_3", "abc"])
    profiles = api_client.get(f"/api/v1/projects/{pid}/exports/profiles").json()
    assert profiles[0]["profile_id"] == "nanounet" and profiles[0]["is_default"] is True
    # Omitting profile_id uses the default profile.
    record = api_client.post(f"/api/v1/projects/{pid}/exports", json={}).json()
    assert record["profile_id"] == "nanounet"
    assert (
        record["options"]["raster_format"] == "tiff"
        and record["options"]["color_visualization"] is True
    )
    stems = {d["pairing_key"]: d["export_stem"] for d in record["datasets"]}
    # 242_2 and 242_3 collide on the loader's first digit run: sequence prefix.
    assert stems == {"242_2": "001_242_2", "242_3": "002_242_3", "abc": "003_abc"}
    entry = next(d for d in record["datasets"] if d["pairing_key"] == "242_2")
    assert entry["original_filenames"] == {
        "topography": "242_2_topography.tiff",
        "friction": "242_2_friction.tiff",
    }
    assert entry["files"] == {
        "topography": "Topography/001_242_2.png",
        "friction": "Friction/001_242_2.png",
        "mask": "Masks_instance/001_242_2.tif",
        "mask_color": "Masks_color/001_242_2.png",
    }
    package = api_client.get(
        f"/api/v1/projects/{pid}/exports/{record['export_id']}/download"
    ).content
    archive = zipfile.ZipFile(io.BytesIO(package))
    names = archive.namelist()
    assert not any("instance_mask" in n for n in names)  # one mask per sample only
    out = tmp_path / "unpacked"
    archive.extractall(out)
    triplets = build_file_triplets(
        str(out / "Topography"), str(out / "Friction"), str(out / "Masks_instance")
    )
    assert [extract_id(t[2]) for t in triplets] == ["001", "002", "003"]
    gt = load_instance_mask(triplets[0][2])
    assert set(np.unique(gt).tolist()) == {classes["Layer A"]["class_id"], 9}
    manifest = json.loads(archive.read("manifest.json"))
    legend = {e["export_value"]: e for e in manifest["color_legend"]}
    assert legend[9]["display_color"] == "#00a398" and legend[0]["rgb"] == [0, 0, 0]
    assert manifest["naming"]["sequence_prefix_applied"] is True
    assert manifest["classes"][1]["export_value"] == 9
    color = np.array(Image.open(io.BytesIO(archive.read("Masks_color/001_242_2.png"))))
    assert color.shape == (64, 64, 3)
    assert tuple(color[15, 15]) == (0x00, 0xA3, 0x98) and tuple(color[0, 0]) == (0xFA, 0x32, 0x53)
    # Same composition: colour pixel <-> machine value everywhere.
    is_b = (color == np.array([0x00, 0xA3, 0x98])).all(axis=2)
    assert np.array_equal(is_b, gt == 9)

    # PNG raster format, no colour visualisation, unique digit runs keep original stems.
    record2 = api_client.post(
        f"/api/v1/projects/{pid}/exports",
        json={
            "dataset_ids": [ids[0], ids[2]],
            "options": {"raster_format": "png", "color_visualization": False},
        },
    ).json()
    files = {d["pairing_key"]: d["files"] for d in record2["datasets"]}
    assert (
        files["242_2"]["mask"] == "Masks_instance/242_2.png" and "mask_color" not in files["242_2"]
    )
    assert files["abc"]["mask"] == "Masks_instance/abc.png"
    archive2 = zipfile.ZipFile(
        io.BytesIO(
            api_client.get(
                f"/api/v1/projects/{pid}/exports/{record2['export_id']}/download"
            ).content
        )
    )
    png_mask = np.array(Image.open(io.BytesIO(archive2.read("Masks_instance/242_2.png"))))
    assert png_mask.dtype == np.uint16 and np.array_equal(png_mask.astype(np.int32), gt)
    # Re-export of the same selection is byte-identical for the mask.
    record3 = api_client.post(
        f"/api/v1/projects/{pid}/exports",
        json={
            "dataset_ids": [ids[0], ids[2]],
            "options": {"raster_format": "png", "color_visualization": False},
        },
    ).json()
    archive3 = zipfile.ZipFile(
        io.BytesIO(
            api_client.get(
                f"/api/v1/projects/{pid}/exports/{record3['export_id']}/download"
            ).content
        )
    )
    assert archive3.read("Masks_instance/242_2.png") == archive2.read("Masks_instance/242_2.png")

    # Native profile keeps richer artefacts and writes the colour mask too.
    native = api_client.post(
        f"/api/v1/projects/{pid}/exports",
        json={"profile_id": "native_exact", "dataset_ids": [ids[0]]},
    ).json()
    files = native["datasets"][0]["files"]
    assert files["final_mask"] == "datasets/242_2/final_mask.tif"
    assert files["instance_mask"] == "datasets/242_2/instance_mask.tif"
    assert files["mask_color"] == "datasets/242_2/mask_color.png"


# ------------------------------------------------------ datasets & help


def test_dataset_search_query_matches_metadata(api_client: TestClient, tmp_path: Path) -> None:
    pid = create_project(api_client)["project_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["242_2", "243", "500"], revision=1)
    dsid = result["datasets"][1]["dataset_id"]
    assert [
        d["pairing_key"] for d in api_client.get(f"/api/v1/projects/{pid}/datasets?q=242_2").json()
    ] == ["242_2"]
    assert [
        d["pairing_key"]
        for d in api_client.get(f"/api/v1/projects/{pid}/datasets?q=FRICTION").json()
    ] == ["242_2", "243", "500"]
    assert [
        d["pairing_key"]
        for d in api_client.get(f"/api/v1/projects/{pid}/datasets?q={dsid[-6:]}").json()
    ] == ["243"]
    assert api_client.get(f"/api/v1/projects/{pid}/datasets?q=nothing").json() == []
    assert len(api_client.get(f"/api/v1/projects/{pid}/datasets").json()) == 3


def test_help_pages_and_registry_are_served_locally(api_client: TestClient) -> None:
    index = api_client.get("/help")
    assert (
        index.status_code == 200
        and "User guide" in index.text
        and "keyboard shortcuts" in index.text.lower()
    )
    # The shortcut table shows the key strings, not ``dict.keys`` (Jinja
    # resolves ``s.keys`` to the method before the item).
    assert "built-in method" not in index.text
    assert "<code>Ctrl+Z</code>" in index.text
    page = api_client.get("/help/user-guide")
    assert page.status_code == 200
    assert 'id="fill-tool"' in page.text and 'id="channels"' in page.text
    assert 'id="subtract-by-order"' in page.text and "Subtract by Order" in page.text
    assert "cdn." not in page.text and "https://" not in page.text.split("<article")[1].split(
        "</article>"
    )[0].replace("https://code.it4i.cz", "")
    missing = api_client.get("/help/does-not-exist")
    assert missing.status_code == 404 and "Document not found" in missing.text
    assert api_client.get("/help/../etc/passwd").status_code in (404, 400)
    registry = api_client.get("/api/v1/help/registry").json()
    assert "view.brightness" in registry["entries"] and registry["shortcuts"]
    assert (
        "view only" in registry["entries"]["view.brightness"]["short"].lower()
        or "only the displayed" in registry["entries"]["view.brightness"]["short"]
    )
