# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Multi-channel projects: channel definitions, N-channel import, per-channel
images, the dataset lock on channel edits and the legacy two-channel API."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import create_project
from tests.fixtures.images import synthetic_friction, synthetic_topography, write_tiff


def _upload(client: TestClient, pid: str, batch_id: str, channel: str, files: list[Path]) -> dict:
    handles = [("files", (f.name, f.open("rb"), "image/tiff")) for f in files]
    try:
        response = client.post(
            f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/files?channel={channel}",
            files=handles,
        )
    finally:
        for _, (_, handle, _) in handles:
            handle.close()
    assert response.status_code == 200, response.text
    return response.json()


def test_project_channel_count_and_defaults(api_client: TestClient) -> None:
    default = create_project(api_client, "pair")
    assert [c["key"] for c in default["channels"]] == ["topography", "friction"]
    assert default["schema_version"] == 2

    response = api_client.post("/api/v1/projects", json={"name": "multi", "channel_count": 4})
    assert response.status_code == 201, response.text
    project = response.json()
    assert [c["key"] for c in project["channels"]] == [
        "topography",
        "friction",
        "channel_3",
        "channel_4",
    ]
    assert project["channels"][2]["label"] == "Channel 3"
    assert "ch3" in project["channels"][2]["filename_tokens"]

    single = api_client.post("/api/v1/projects", json={"name": "single", "channel_count": 1})
    assert single.status_code == 201 and len(single.json()["channels"]) == 1

    for bad in (0, 65):
        assert (
            api_client.post(
                "/api/v1/projects", json={"name": "x", "channel_count": bad}
            ).status_code
            == 422
        )

    listing = api_client.get("/api/v1/projects").json()
    by_name = {p["name"]: p for p in listing}
    assert by_name["multi"]["channel_count"] == 4
    assert by_name["pair"]["first_channel_key"] == "topography"


def test_channels_editable_only_without_datasets(api_client: TestClient, tmp_path: Path) -> None:
    project = api_client.post("/api/v1/projects", json={"name": "three", "channel_count": 3}).json()
    pid = project["project_id"]
    channels = [
        {"key": "topography", "label": "Height", "filename_tokens": ["topography", "height"]},
        {"key": "friction", "label": "Friction", "filename_tokens": ["friction"]},
        {"key": "phase", "label": "Phase", "filename_tokens": ["phase", "ph"]},
    ]
    patched = api_client.patch(
        f"/api/v1/projects/{pid}", json={"expected_revision": 1, "channels": channels}
    )
    assert patched.status_code == 200, patched.text
    assert [c["label"] for c in patched.json()["channels"]] == ["Height", "Friction", "Phase"]

    duplicate = api_client.patch(
        f"/api/v1/projects/{pid}",
        json={
            "expected_revision": 2,
            "channels": [
                {"key": "a", "label": "A", "filename_tokens": ["x"]},
                {"key": "b", "label": "B", "filename_tokens": ["x"]},
            ],
        },
    )
    assert duplicate.status_code == 422 and "token" in duplicate.json()["error"]["message"]

    # Import a 3-channel dataset: one file per channel, same pairing key.
    d = tmp_path / "in"
    t = write_tiff(d / "s07_height.tiff", synthetic_topography(48, 48))
    f = write_tiff(d / "s07_friction.tiff", synthetic_friction(48, 48))
    p = write_tiff(d / "s07_ph.tiff", synthetic_friction(48, 48, seed=5))
    t2 = write_tiff(d / "s08_height.tiff", synthetic_topography(48, 48))
    f2 = write_tiff(d / "s08_friction.tiff", synthetic_friction(48, 48))
    batch_id = api_client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    _upload(api_client, pid, batch_id, "topography", [t, t2])
    _upload(api_client, pid, batch_id, "friction", [f, f2])
    preview = _upload(api_client, pid, batch_id, "phase", [p])
    assert preview["channel_keys"] == ["topography", "friction", "phase"]
    rows = {r["pairing_key"]: r for r in preview["rows"]}
    assert rows["s07"]["status"] == "ok"
    assert set(rows["s07"]["files"]) == {"topography", "friction", "phase"}
    assert rows["s08"]["status"] == "missing"
    assert rows["s08"]["missing_channels"] == ["phase"]
    assert rows["s08"]["message"] == "missing Phase"
    assert preview["ok_count"] == 1 and preview["problem_count"] == 1

    unknown = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/files?channel=amplitude",
        files=[("files", ("x.tiff", b"1234", "image/tiff"))],
    )
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "CHANNEL_NOT_FOUND"

    # Committing a row that misses a channel is refused; the OK row works.
    missing = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={
            "expected_revision": 2,
            "pairs": [
                {
                    "pairing_key": "s08",
                    "files": {
                        "topography": rows["s08"]["files"]["topography"]["file_id"],
                        "friction": rows["s08"]["files"]["friction"]["file_id"],
                    },
                }
            ],
        },
    )
    assert missing.status_code == 422
    commit = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={
            "expected_revision": 2,
            "pairs": [
                {
                    "pairing_key": "s07",
                    "files": {k: v["file_id"] for k, v in rows["s07"]["files"].items()},
                }
            ],
        },
    )
    assert commit.status_code == 200, commit.text
    summary = commit.json()["datasets"][0]
    assert [s["channel"] for s in summary["sources"]] == ["topography", "friction", "phase"]
    assert summary["sources"][2]["original_filename"] == "s07_ph.tiff"
    dsid = summary["dataset_id"]

    detail = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}").json()["dataset"]
    assert [c["channel"] for c in detail["channels"]] == ["topography", "friction", "phase"]
    assert detail["channels"][2]["stored_filename"] == "phase.tiff"
    for channel in ("topography", "friction", "phase"):
        image = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/image/{channel}")
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        thumb = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/thumbnail/{channel}")
        assert thumb.status_code == 200
    nope = api_client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/image/amplitude")
    assert nope.status_code == 404 and nope.json()["error"]["code"] == "CHANNEL_NOT_FOUND"

    # The workspace page renders the 3-channel navigator.
    page = api_client.get(f"/projects/{pid}/datasets/{dsid}")
    assert page.status_code == 200
    assert 'data-channel-count="3"' in page.text and 'id="channel-slider"' in page.text
    assert "Phase" in page.text

    # Channels are locked once datasets exist.
    revision = api_client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    locked = api_client.patch(
        f"/api/v1/projects/{pid}", json={"expected_revision": revision, "channels": channels}
    )
    assert locked.status_code == 409 and locked.json()["error"]["code"] == "PROJECT_INVALID"

    # Search matches the third channel's filename.
    found = api_client.get(f"/api/v1/projects/{pid}/datasets?q=s07_ph").json()
    assert [d["dataset_id"] for d in found] == [dsid]


def test_two_channel_project_renders_buttons_and_single_channel_no_switch(
    api_client: TestClient, tmp_path: Path
) -> None:
    single = api_client.post("/api/v1/projects", json={"name": "one", "channel_count": 1}).json()
    pid = single["project_id"]
    d = tmp_path / "in"
    a = write_tiff(d / "scan_01.tiff", synthetic_topography(32, 32))
    b = write_tiff(d / "scan_02.tiff", synthetic_topography(32, 32, seed=3))
    batch_id = api_client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    preview = _upload(api_client, pid, batch_id, "topography", [a, b])
    assert preview["ok_count"] == 2
    commit = api_client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch_id}/commit",
        json={
            "expected_revision": 1,
            "pairs": [
                {
                    "pairing_key": r["pairing_key"],
                    "files": {"topography": r["files"]["topography"]["file_id"]},
                }
                for r in preview["rows"]
            ],
        },
    )
    assert commit.status_code == 200, commit.text
    dsid = commit.json()["datasets"][0]["dataset_id"]
    page = api_client.get(f"/projects/{pid}/datasets/{dsid}")
    assert 'data-channel-count="1"' in page.text and "channel-slider" not in page.text
