# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests: overlap Findings, navigation, drag-and-drop z-order,
missing-pixel Findings and validation from the Findings panel (updated for
the unified Findings panel)."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_workspace import _cookies, _prepare_project

pytestmark = pytest.mark.browser


def _rows(width: int, height: int, y0: int, y1: int, x0: int, x1: int) -> dict:
    counts: list[int] = []
    value = 0
    run = 0
    for y in range(height):
        for x in range(width):
            v = 1 if y0 <= y < y1 and x0 <= x < x1 else 0
            if v == value:
                run += 1
            else:
                counts.append(run)
                value = v
                run = 1
    counts.append(run)
    return {"format": "rle_rows", "size": [height, width], "counts": counts}


def test_overlaps_findings_and_zorder_drag(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    headers = {"X-Requested-With": "AFMLabel"}
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=headers, timeout=30
    ) as client:
        base = f"/api/v1/projects/{pid}/datasets/{dsid}"
        classes = client.get(f"/api/v1/projects/{pid}/classes").json()["classes"]
        a, b = classes[0]["class_id"], classes[1]["class_id"]
        client.post(
            f"{base}/regions",
            json={"expected_revision": 1, "class_id": a, "mask": _rows(64, 64, 0, 64, 0, 64)},
        )
        client.post(
            f"{base}/regions",
            json={"expected_revision": 2, "class_id": b, "mask": _rows(64, 64, 20, 30, 20, 30)},
        )
        client.post(
            f"{base}/regions",
            json={"expected_revision": 3, "class_id": a, "mask": _rows(64, 64, 22, 26, 22, 26)},
        )

    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(3)
    expect(page.locator("#status-overlaps")).to_contain_text("Overlaps: 1 (max ×3, 1 unreviewed)")
    expect(page.locator("#findings-count")).to_have_text("1")
    expect(page.locator("#status-coverage")).to_contain_text("100.00 %")

    # Clicking the overlap Finding navigates (zoom changes) and highlights participants.
    page.click("#status-overlaps")
    expect(page.locator("#panel-findings")).to_be_visible()
    zoom_before = page.evaluate("document.getElementById('zoom-value').textContent")
    overlap = page.locator('#findings-list .lv-finding-item[data-finding-type="overlap"]')
    expect(overlap).to_have_count(1)
    overlap.click()
    expect(overlap).to_contain_text("×3")
    assert page.evaluate("document.getElementById('zoom-value').textContent") != zoom_before
    expect(page.locator("#region-list .lv-region-item.in-overlap")).to_have_count(3)

    # Drag the bottom region (reg_000001, listed last) to the top of the list.
    page.click("#tab-regions")
    items = page.locator("#region-list .lv-region-item")
    expect(items.first).to_have_attribute("data-region-id", "reg_000003")
    items.last.locator(".lv-region-name").drag_to(items.first.locator(".lv-region-name"))
    expect(page.locator("#region-list .lv-region-item").first).to_have_attribute(
        "data-region-id", "reg_000001"
    )
    expect(page.locator("#save-state")).to_have_text("Saved")
    page.click("#tab-findings")
    page.click("#refresh-findings-button")
    expect(overlap).to_contain_text("winner: reg_000001")

    # Review the overlap (review state only), validate from the panel: valid.
    page.click('#findings-list button[data-state="reviewed_intentional"]')
    expect(page.locator("#status-overlaps")).to_contain_text("Overlaps: 1 (max ×3)")
    expect(overlap).to_have_class(re.compile(r"review-reviewed"))
    page.click("#findings-validate-button")
    expect(page.locator("#validation-report")).to_contain_text("VALID")
    expect(page.locator("#status-validation")).to_contain_text("valid at r")
    expect(page.locator("#findings-stale")).to_be_hidden()

    # Erase a hole: the missing-coverage Finding appears, validation is stale.
    page.click("#tab-regions")
    page.click("#region-list .lv-region-item >> nth=0 >> .lv-region-name")
    page.keyboard.press("f")  # fit: erase far from the overlap group
    page.keyboard.press("e")
    for _ in range(9):
        page.keyboard.press("]")
    box = page.locator("#annotation-canvas").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx + 150, cy + 150)
    page.mouse.down()
    page.mouse.move(cx + 170, cy + 170, steps=3)
    page.mouse.up()
    expect(page.locator("#save-state")).to_have_text("Saved")
    expect(page.locator("#status-missing")).not_to_contain_text("Missing: 0 px")
    expect(page.locator("#status-validation")).to_contain_text("stale")
    page.click("#status-missing")
    expect(page.locator("#findings-stale")).to_be_visible()
    expect(
        page.locator('#findings-list .lv-finding-item[data-finding-type="missing_coverage"]').first
    ).to_be_visible()
    page.click("#findings-validate-button")
    expect(page.locator("#validation-report")).to_contain_text("UNLABELED_PIXELS")
