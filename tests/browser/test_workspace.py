# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for the annotation workspace."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.images import write_pair

pytestmark = pytest.mark.browser

SELECTED_AREA_JS = (
    "Number(document.querySelector("
    "'#region-list .lv-region-item.selected .lv-region-meta'"
    r").textContent.match(/(\d+) px/)[1])"
)


def _prepare_project(base_url: str, cookies: dict, tmp_path: Path) -> tuple[str, str]:
    headers = {"X-Requested-With": "AFMLabel"}
    with httpx.Client(base_url=base_url, cookies=cookies, headers=headers, timeout=30) as client:
        pid = client.post("/api/v1/projects", json={"name": "Workspace project"}).json()[
            "project_id"
        ]
        rev = client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": 1, "name": "Layer A"}
        ).json()["project"]["revision"]
        rev = client.post(
            f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": "Layer B"}
        ).json()["project"]["revision"]
        batch = client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
        topo, fric = write_pair(tmp_path, "801")
        with topo.open("rb") as t:
            client.post(
                f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/files?channel=topography",
                files=[("files", (topo.name, t, "image/tiff"))],
            )
        with fric.open("rb") as f:
            preview = client.post(
                f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/files?channel=friction",
                files=[("files", (fric.name, f, "image/tiff"))],
            ).json()
        row = preview["rows"][0]
        result = client.post(
            f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/commit",
            json={
                "expected_revision": rev,
                "pairs": [
                    {
                        "pairing_key": row["pairing_key"],
                        "topography_file_id": row["files"]["topography"]["file_id"],
                        "friction_file_id": row["files"]["friction"]["file_id"],
                    }
                ],
            },
        ).json()
        return pid, result["datasets"][0]["dataset_id"]


def _cookies(page: Page) -> dict:
    return {c["name"]: c["value"] for c in page.context.cookies()}


def _canvas_center(page: Page) -> tuple[float, float]:
    box = page.locator("#annotation-canvas").bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def test_brush_polygon_channel_and_persistence(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("#lock-indicator")).to_contain_text("held by this tab")
    expect(page.locator("#status-coverage")).to_contain_text("0.00 %")

    # Brush stroke with no selection creates a region of the active class.
    page.keyboard.press("b")
    cx, cy = _canvas_center(page)
    page.mouse.move(cx - 30, cy)
    page.mouse.down()
    page.mouse.move(cx + 30, cy, steps=8)
    page.mouse.up()
    expect(page.locator("#region-list .lv-region-item")).to_have_count(1)
    expect(page.locator("#save-state")).to_have_text("Saved")
    expect(page.locator("#dataset-revision")).to_have_text("2")

    # Polygon creates a second region; switching channel keeps everything.
    page.keyboard.press("Escape")
    page.keyboard.press("p")
    for dx, dy in ((-140, -140), (-20, -140), (-20, -20), (-140, -20)):
        page.mouse.click(cx + dx, cy + dy)
    page.keyboard.press("Enter")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    zoom_before = page.evaluate("document.getElementById('zoom-value').textContent")
    page.click('#channel-switch button[data-channel="friction"]')
    expect(page.locator('#channel-switch button[data-channel="friction"]')).to_have_class(
        "active"
    )
    assert page.evaluate("document.getElementById('zoom-value').textContent") == zoom_before
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)

    # Eraser on the selected region reduces its area; undo restores it.
    area_before = page.evaluate(SELECTED_AREA_JS)
    page.keyboard.press("e")
    for _ in range(8):  # brush radius 10 -> 2 image px
        page.keyboard.press("[")
    page.mouse.move(cx - 135, cy - 135)
    page.mouse.down()
    page.mouse.move(cx - 125, cy - 125, steps=4)
    page.mouse.up()
    expect(page.locator("#save-state")).to_have_text("Saved")
    area_after = page.evaluate(SELECTED_AREA_JS)
    assert area_after < area_before
    page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#save-state")).to_have_text("Saved")
    area_restored = page.evaluate(SELECTED_AREA_JS)
    assert area_restored == area_before

    # Reload: regions, channel and tool come back from persisted state.
    page.wait_for_timeout(1200)
    page.reload()
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    expect(page.locator('#channel-switch button[data-channel="friction"]')).to_have_class(
        "active"
    )
    expect(page.locator('.tool-button[data-tool="eraser"]')).to_have_class("tool-button active")
    expect(page.locator("#status-coverage")).not_to_contain_text("0.00 %")

    # SAM tools stay disabled: no backend advertises point prompts yet? The
    # fake backend does, so they are enabled when it is selected.
    expect(page.locator('.tool-button[data-tool="sam_positive"]')).to_be_enabled()
