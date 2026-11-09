# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for large-project navigation and import/export:
dataset search finds `242_2`, clears, stays
usable with 500+ datasets, Previous/Next follow the filter; export dialog
defaults to NanoUNet, has search, names files from original filenames
and downloads a colour mask; flattened-mask import proposes Buffer, has no
`Skip` class and opens the workspace with the imported regions selected."""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import httpx
import numpy as np
import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_regions_editing import HEADERS
from tests.browser.test_workspace import _cookies
from tests.fixtures.images import write_pair, write_png

pytestmark = pytest.mark.browser


def _import_pairs(
    client: httpx.Client, pid: str, directory: Path, keys: list[str], size: int = 16
) -> list[str]:
    batch = client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
    files_t = []
    files_f = []
    for key in keys:
        topo, fric = write_pair(directory, key, height=size, width=size)
        files_t.append(topo)
        files_f.append(fric)
    for modality, paths in (("topography", files_t), ("friction", files_f)):
        for chunk_start in range(0, len(paths), 100):
            chunk = paths[chunk_start : chunk_start + 100]
            handles = [p.open("rb") for p in chunk]
            try:
                client.post(
                    f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/files?channel={modality}",
                    files=[
                        ("files", (p.name, h, "image/tiff"))
                        for p, h in zip(chunk, handles, strict=True)
                    ],
                )
            finally:
                for h in handles:
                    h.close()
    preview = client.get(f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}").json()
    revision = client.get(f"/api/v1/projects/{pid}").json()["project"]["revision"]
    pairs = [
        {
            "pairing_key": row["pairing_key"],
            "topography_file_id": row["files"]["topography"]["file_id"],
            "friction_file_id": row["files"]["friction"]["file_id"],
        }
        for row in preview["rows"]
        if row["status"] == "ok"
    ]
    response = client.post(
        f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/commit",
        json={"expected_revision": revision, "pairs": pairs},
    )
    assert response.status_code == 200, f"{len(pairs)} pairs: {response.text[:500]}"
    return [d["dataset_id"] for d in response.json()["datasets"]]


def test_dataset_search_with_500_datasets_and_filtered_navigation(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=600
    ) as client:
        pid = client.post("/api/v1/projects", json={"name": "Large project"}).json()["project_id"]
        client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": 1, "name": "Layer A"}
        )
        keys = [f"{200 + i // 3}_{i % 3 + 1}" for i in range(501)]
        ids = _import_pairs(client, pid, tmp_path / "in", keys)
        assert len(ids) == 501
        # Server-side metadata filter agrees with the client filter.
        assert [
            d["pairing_key"] for d in client.get(f"/api/v1/projects/{pid}/datasets?q=242_2").json()
        ] == ["242_2"]

    started = time.monotonic()
    page.goto(f"{live_server_url}/projects/{pid}")
    expect(page.locator("#dataset-count")).to_have_text("501")
    expect(page.locator(".lv-dataset-card")).to_have_count(501)
    assert time.monotonic() - started < 60
    search = page.locator("#dataset-search")
    search.fill("242_2")
    expect(page.locator("#dataset-filter-count")).to_have_text("1 of 501 match")
    visible = page.locator(".lv-dataset-card:not([hidden])")
    expect(visible).to_have_count(1)
    expect(visible.first).to_contain_text("242_2")
    search.fill("242_")  # "242" alone could match a random hex dataset id
    expect(page.locator("#dataset-filter-count")).to_have_text("3 of 501 match")
    expect(page.locator(".lv-dataset-card:not([hidden])")).to_have_count(3)
    search.fill("no-such-dataset")
    expect(page.locator("#dataset-no-results")).to_be_visible()
    search.fill("242_")  # "242" alone could match a random hex dataset id

    # Open a dataset: Next/Previous follow the filtered list; filter survives.
    page.locator(".lv-dataset-card:not([hidden]) .lv-open-button").first.click()
    expect(page.locator("#dataset-filter-note")).to_contain_text('filter "242_": 1 / 3')
    expect(page.locator("#prev-dataset-button")).to_have_attribute("aria-disabled", "true")
    page.keyboard.press("PageDown")
    expect(page.locator("#dataset-title")).to_have_text("242_2")
    expect(page.locator("#dataset-filter-note")).to_contain_text("2 / 3")
    page.keyboard.press("PageDown")
    expect(page.locator("#dataset-title")).to_have_text("242_3")
    expect(page.locator("#next-dataset-button")).to_have_attribute("aria-disabled", "true")
    page.click(".lv-job-back")
    expect(page.locator("#dataset-search")).to_have_value("242_")
    page.click("#dataset-search-clear")
    expect(page.locator("#dataset-search")).to_have_value("")
    expect(page.locator("#dataset-filter-count")).to_have_text("501 dataset(s)")
    expect(page.locator(".lv-dataset-card:not([hidden])")).to_have_count(501)


def test_export_dialog_defaults_search_naming_and_color_mask(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=60
    ) as client:
        pid = client.post("/api/v1/projects", json={"name": "Export project"}).json()["project_id"]
        created = client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": 1, "name": "Layer A"}
        ).json()
        class_a = created["classes"]["classes"][0]["class_id"]
        ids = _import_pairs(client, pid, tmp_path / "in", ["242_2", "243_1"], size=16)
        for dsid in ids:
            base = f"/api/v1/projects/{pid}/datasets/{dsid}"
            client.post(
                f"{base}/regions",
                json={
                    "expected_revision": 1,
                    "class_id": class_a,
                    "mask": {"format": "rle_rows", "size": [16, 16], "counts": [0, 256]},
                },
            )
            assert client.post(f"{base}/validate").json()["status"] == "valid"

    page.goto(f"{live_server_url}/projects/{pid}")
    page.click("#open-export-button")
    expect(page.locator("#export-modal")).to_be_visible()
    expect(page.locator("#export-profile")).to_have_value("nanounet")
    expect(page.locator("#export-profile option").first).to_contain_text("default")
    expect(page.locator("#export-dataset-list li[data-dataset-id]")).to_have_count(2)
    page.fill("#export-search", "243")
    expect(page.locator("#export-filter-count")).to_have_text("1 of 2 shown")
    expect(page.locator("#export-dataset-list li[data-dataset-id]:not([hidden])")).to_have_count(1)
    page.fill("#export-search", "validated")
    expect(page.locator("#export-dataset-list li[data-dataset-id]:not([hidden])")).to_have_count(2)
    page.fill("#export-search", "")
    preview = page.locator("#export-filename-preview")
    expect(preview).to_be_visible()
    expect(preview).to_contain_text("242_2_topography.tiff + 242_2_friction.tiff")
    expect(preview).to_contain_text("Masks_instance/242_2.tif")
    expect(preview).to_contain_text("Masks_color/242_2.png")
    page.select_option("#export-raster-format", "png")
    expect(preview).to_contain_text("Masks_instance/242_2.png")
    page.click("#export-run-button")
    expect(page.locator("#export-status")).to_contain_text("2 dataset(s) exported")
    with page.expect_download() as download_info:
        page.click("#export-history a.lv-btn")
    target = tmp_path / "export.zip"
    download_info.value.save_as(target)
    archive = zipfile.ZipFile(target)
    names = set(archive.namelist())
    assert {
        "Topography/242_2.png",
        "Friction/242_2.png",
        "Masks_instance/242_2.png",
        "Masks_color/242_2.png",
    } <= names
    assert {"Masks_instance/243_1.png", "Masks_color/243_1.png"} <= names
    manifest = json.loads(archive.read("manifest.json"))
    entry = next(d for d in manifest["datasets"] if d["export_stem"] == "242_2")
    assert entry["original_filenames"] == {
        "topography": "242_2_topography.tiff",
        "friction": "242_2_friction.tiff",
    }
    assert (
        manifest["color_legend"][1]["display_color"]
        == created["classes"]["classes"][0]["display_color"]
    )
    from PIL import Image

    color = np.array(Image.open(io.BytesIO(archive.read("Masks_color/242_2.png"))))
    assert color.shape == (16, 16, 3)
    expected = tuple(
        int(created["classes"]["classes"][0]["display_color"].lstrip("#")[i : i + 2], 16)
        for i in (0, 2, 4)
    )
    assert {tuple(px) for px in color.reshape(-1, 3)} == {expected}


def test_flattened_mask_import_defaults_to_buffer_and_selects_result(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=60
    ) as client:
        pid = client.post("/api/v1/projects", json={"name": "Import project"}).json()["project_id"]
        rev = client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": 1, "name": "SLG"}
        ).json()["project"]["revision"]
        created = client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": "buffer"}
        ).json()
        buffer_id = created["classes"]["classes"][-1]["class_id"]
        (dsid,) = _import_pairs(client, pid, tmp_path / "in", ["300"], size=32)
    labels = np.zeros((32, 32), dtype=np.uint8)
    labels[4:12, 4:12] = 1
    labels[20:28, 20:28] = 2
    raster = write_png(tmp_path / "flat.png", labels)
    wrong = write_png(tmp_path / "wrong.png", np.ones((8, 8), dtype=np.uint8))

    page.goto(f"{live_server_url}/projects/{pid}")
    page.click('.lv-dataset-card [data-action="lossy"]')
    expect(page.locator("#lossy-import-modal")).to_be_visible()
    expect(page.locator("#lossy-expected-size")).to_have_text("32 × 32")
    # Dimension mismatch is refused with expected/received sizes.
    page.set_input_files("#lossy-file", str(wrong))
    page.click("#lossy-stage-button")
    expect(page.locator("#lossy-error")).to_contain_text("Expected: 32 x 32")
    expect(page.locator("#lossy-error")).to_contain_text("Received: 8 x 8")
    expect(page.locator("#lossy-commit-button")).to_be_disabled()

    page.set_input_files("#lossy-file", str(raster))
    page.click("#lossy-stage-button")
    selects = page.locator("#lossy-values-body select")
    expect(selects).to_have_count(2)
    expect(page.locator("#lossy-default-note")).to_contain_text("buffer (buffer class)")
    for i in range(2):
        expect(selects.nth(i)).to_have_value(str(buffer_id))
        options = [o.text_content() for o in selects.nth(i).locator("option").all()]
        assert not any(o.strip().lower() == "skip" for o in options)
        assert any("Do not import this value" in o for o in options)
    expect(page.locator("#lossy-commit-button")).to_be_enabled()
    # Discarding a value requires an explicit confirmation.
    selects.nth(1).select_option("__discard__")
    expect(page.locator("#lossy-discard-row")).to_be_visible()
    expect(page.locator("#lossy-commit-button")).to_be_disabled()
    page.check("#lossy-confirm-discard")
    expect(page.locator("#lossy-commit-button")).to_be_enabled()
    selects.nth(1).select_option(str(buffer_id))
    expect(page.locator("#lossy-discard-row")).to_be_hidden()

    page.click("#lossy-commit-button")
    page.wait_for_url(f"{live_server_url}/projects/{pid}/datasets/{dsid}**")
    expect(page.locator("body")).to_have_attribute("data-ready", "1")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(2)
    expect(page.locator("#action-message")).to_contain_text("Imported 2 region(s)")
    assert page.evaluate("window.__afmlabelWorkspace.activeClassId()") == buffer_id
    expect(page.locator("#region-list .lv-region-item").first).to_contain_text("lossy_import")
    # One undo removes the whole import; redo brings it back with the same ids.
    ids = page.evaluate("window.__afmlabelWorkspace.selectedIds()")
    page.keyboard.press("Control+z")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(0)
    page.keyboard.press("Control+y")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=30
    ) as client:
        regions = client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/regions").json()["regions"]
    assert sorted(r["region_id"] for r in regions) == sorted(ids)
