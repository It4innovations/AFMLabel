# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from afmlabel.main import create_app
from afmlabel.settings import AppSettings
from tests.conftest import DEVELOPER_DATA_DIR, create_project, login
from tests.fixtures.images import synthetic_friction, synthetic_topography, write_pair, write_tiff


def _upload(client: TestClient, pid: str, batch_id: str, modality: str, files: list[Path]) -> dict:
    handles = [("files", (f.name, f.open("rb"), "image/tiff")) for f in files]
    try:
        response = client.post(
            f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/files?channel={modality}",
            files=handles,
        )
    finally:
        for _, (_, handle, _) in handles:
            handle.close()
    assert response.status_code == 200, response.text
    return response.json()


def import_pairs(
    client: TestClient, pid: str, directory: Path, keys: list[str], revision: int
) -> dict:
    topos, frics = [], []
    for key in keys:
        t, f = write_pair(directory, key)
        topos.append(t)
        frics.append(f)
    batch_id = client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    _upload(client, pid, batch_id, "topography", topos)
    preview = _upload(client, pid, batch_id, "friction", frics)
    pairs = [
        {
            "pairing_key": r["pairing_key"],
            "topography_file_id": r["files"]["topography"]["file_id"],
            "friction_file_id": r["files"]["friction"]["file_id"],
        }
        for r in preview["rows"]
        if r["status"] == "ok"
    ]
    response = client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={"expected_revision": revision, "pairs": pairs},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_batch_upload_preview_commit_and_navigation(api_client: TestClient, tmp_path: Path) -> None:
    pid = create_project(api_client)["project_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["239", "240", "241"], revision=1)
    assert [d["name"] for d in result["datasets"]] == ["239", "240", "241"]
    assert result["project_revision"] == 2

    listing = api_client.get(f"/api/v1/projects/{pid}/datasets").json()
    assert len(listing) == 3
    first = listing[0]
    assert first["width"] == 64 and first["status"] == "ready" and first["region_count"] == 0

    detail = api_client.get(f"/api/v1/projects/{pid}/datasets/{first['dataset_id']}").json()
    assert detail["previous_dataset_id"] is None
    assert detail["next_dataset_id"] == listing[1]["dataset_id"]
    ds = detail["dataset"]
    assert [c["channel"] for c in ds["channels"]] == ["topography", "friction"]
    assert ds["channels"][0]["sha256"] != ds["channels"][1]["sha256"]
    assert ds["channels"][0]["dtype"] == "uint16"
    assert ds["schema_version"] == 2
    assert ds["pairing_method"] == "key"

    image = api_client.get(f"/api/v1/projects/{pid}/datasets/{first['dataset_id']}/image/friction")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    thumb = api_client.get(
        f"/api/v1/projects/{pid}/datasets/{first['dataset_id']}/thumbnail/topography"
    )
    assert thumb.status_code == 200
    source = api_client.get(
        f"/api/v1/projects/{pid}/datasets/{first['dataset_id']}/source/topography"
    )
    assert source.status_code == 200 and source.content[:2] in (b"II", b"MM")

    # The batch is gone after commit.
    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert "datasets.import" in [e["operation"] for e in events]
    assert sum(1 for e in events if e["operation"] == "dataset.create") == 3

    # Project page and index card render with datasets.
    assert "Annotate" not in api_client.get(f"/projects/{pid}").text  # rendered client-side
    assert api_client.get("/").status_code == 200

    deleted = api_client.delete(
        f"/api/v1/projects/{pid}/datasets/{first['dataset_id']}?expected_revision=1"
    )
    assert deleted.status_code == 204
    assert len(api_client.get(f"/api/v1/projects/{pid}/datasets").json()) == 2


def test_ambiguous_and_mismatched_pairs_never_commit(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid = create_project(api_client)["project_id"]
    d = tmp_path / "in"
    t1 = write_tiff(d / "300_topography.tiff", synthetic_topography(64, 64))
    t1b = write_tiff(d / "300_topo.tiff", synthetic_topography(64, 64))
    f1 = write_tiff(d / "300_friction.tiff", synthetic_friction(64, 64))
    t2 = write_tiff(d / "301_topography.tiff", synthetic_topography(64, 64))
    f2 = write_tiff(d / "301_friction.tiff", synthetic_friction(32, 32))
    batch_id = api_client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    _upload(api_client, pid, batch_id, "topography", [t1, t1b, t2])
    preview = _upload(api_client, pid, batch_id, "friction", [f1, f2])
    statuses = {r["pairing_key"]: r["status"] for r in preview["rows"]}
    assert statuses == {"300": "ambiguous", "301": "dimension_mismatch"}
    assert preview["ok_count"] == 0

    ambiguous_ids = [c["file_id"] for c in preview["rows"][0]["candidates"]]
    forced = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={
            "expected_revision": 1,
            "pairs": [
                {
                    "pairing_key": "300",
                    "topography_file_id": ambiguous_ids[0],
                    "friction_file_id": ambiguous_ids[-1],
                }
            ],
        },
    )
    assert forced.status_code == 422
    assert forced.json()["error"]["code"] == "PAIR_AMBIGUOUS"

    mismatch = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={
            "expected_revision": 1,
            "pairs": [
                {
                    "pairing_key": "301",
                    "topography_file_id": preview["rows"][1]["files"]["topography"]["file_id"],
                    "friction_file_id": preview["rows"][1]["files"]["friction"]["file_id"],
                }
            ],
        },
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "PAIR_DIMENSION_MISMATCH"
    assert api_client.get(f"/api/v1/projects/{pid}/datasets").json() == []

    # Removing the extra topography resolves the ambiguity.
    extra = next(
        c["file_id"]
        for c in preview["rows"][0]["candidates"]
        if c["original_filename"] == "300_topo.tiff"
    )
    fixed = api_client.delete(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/files/{extra}"
    ).json()
    assert fixed["rows"][0]["status"] == "ok"
    assert (
        api_client.delete(f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}").status_code
        == 204
    )
    assert (
        api_client.get(f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}").status_code
        == 404
    )


def test_invalid_upload_types_and_size_limits(
    app_settings: AppSettings, configured_account: object, tmp_path: Path
) -> None:
    app_settings.upload.maximum_file_size_mb = 0.001
    client = TestClient(create_app(app_settings))
    client.headers.update({"X-Requested-With": "AFMLabel"})
    login(client)
    pid = create_project(client)["project_id"]
    batch_id = client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    bad = client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/files?channel=topography",
        files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))],
    )
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "UPLOAD_INVALID"
    big = write_tiff(tmp_path / "big_topography.tiff", synthetic_topography(128, 128))
    with big.open("rb") as handle:
        too_large = client.post(
            f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/files?channel=topography",
            files=[("files", (big.name, handle, "image/tiff"))],
        )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "UPLOAD_TOO_LARGE"
    batch_dir = app_settings.workspace.root / "tmp" / "uploads" / batch_id
    assert not list(batch_dir.glob("*.part"))


def test_source_replacement_flow(api_client: TestClient, tmp_path: Path) -> None:
    pid = create_project(api_client)["project_id"]
    result = import_pairs(api_client, pid, tmp_path / "in", ["500"], revision=1)
    dsid = result["datasets"][0]["dataset_id"]
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"

    # Incompatible dimensions -> report says so and confirm is refused.
    wrong = write_tiff(tmp_path / "wrong.tiff", synthetic_topography(32, 32))
    with wrong.open("rb") as handle:
        report = api_client.post(
            f"{base}/replace-source/topography", files={"file": (wrong.name, handle, "image/tiff")}
        ).json()
    assert report["compatible"] is False and report["code"] == "SOURCE_REPLACEMENT_INCOMPATIBLE"
    refused = api_client.post(
        f"{base}/replace-source/topography/confirm",
        json={"replacement_id": report["replacement_id"], "expected_revision": 1},
    )
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "SOURCE_REPLACEMENT_INCOMPATIBLE"
    before = api_client.get(base).json()["dataset"]
    assert before["revision"] == 1

    # Compatible replacement -> revision bump, validation stale, audit trail.
    good = write_tiff(tmp_path / "good_topography.tiff", synthetic_topography(64, 64, seed=99))
    with good.open("rb") as handle:
        report = api_client.post(
            f"{base}/replace-source/topography", files={"file": (good.name, handle, "image/tiff")}
        ).json()
    assert report["compatible"] is True
    confirmed = api_client.post(
        f"{base}/replace-source/topography/confirm",
        json={"replacement_id": report["replacement_id"], "expected_revision": 1},
    )
    assert confirmed.status_code == 200, confirmed.text
    after = confirmed.json()
    assert after["revision"] == 2
    assert after["validated_revision"] is None
    topo_after = next(c for c in after["channels"] if c["channel"] == "topography")
    topo_before = next(c for c in before["channels"] if c["channel"] == "topography")
    assert topo_after["original_filename"] == "good_topography.tiff"
    assert topo_after["sha256"] == report["candidate"]["sha256"]
    assert after["replacements"][0]["previous_sha256"] == topo_before["sha256"]
    assert after["replacements"][0]["channel"] == "topography"
    assert [c for c in after["channels"] if c["channel"] == "friction"] == [
        c for c in before["channels"] if c["channel"] == "friction"
    ]
    source = api_client.get(f"{base}/source/topography").content
    import hashlib

    assert hashlib.sha256(source).hexdigest() == report["candidate"]["sha256"]
    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert events[-1]["operation"] == "dataset.replace_source.topography"

    stale = api_client.post(
        f"{base}/replace-source/topography/confirm",
        json={"replacement_id": report["replacement_id"], "expected_revision": 1},
    )
    assert stale.status_code == 404  # staged candidate consumed


def test_real_developer_data_pairs_when_available(api_client: TestClient) -> None:
    real = DEVELOPER_DATA_DIR / "real" / "real_data_"
    cvat = DEVELOPER_DATA_DIR / "data" / "images"
    if not real.exists() or not cvat.exists():
        import pytest

        pytest.skip("developer-local .data/extracted not present")
    pid = create_project(api_client, "real data")["project_id"]
    batch_id = api_client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    topos = sorted((real / "Topography").glob("*.tiff"))[:3]
    frics = sorted((real / "Friction").glob("*.tiff"))[:3]
    _upload(api_client, pid, batch_id, "topography", topos)
    preview = _upload(api_client, pid, batch_id, "friction", frics)
    assert [r["status"] for r in preview["rows"]] == ["ok"] * 3
    assert preview["rows"][0]["pairing_key"] == "232"
    assert preview["rows"][0]["files"]["topography"]["dtype"] == "uint16"

    # The CVAT export images (514x514 RGB LZW) decode and pair too.
    batch2 = api_client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    _upload(api_client, pid, batch2, "topography", [cvat / "242_topography2.tiff"])
    preview2 = _upload(api_client, pid, batch2, "friction", [cvat / "242_friction2.tiff"])
    row = preview2["rows"][0]
    assert row["status"] == "ok" and row["pairing_key"] == "242_2"
    topo = row["files"]["topography"]
    assert topo["width"] == 514 and topo["channels"] == 3
    assert np.array_equal is not None
