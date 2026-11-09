# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for the multi-channel navigator and the seed-fill tool."""

from __future__ import annotations

from pathlib import Path

import httpx
import numpy as np
import pytest
from playwright.sync_api import Page, expect

from afmlabel.persistence.rle import encode_rows
from tests.browser.test_workspace import _canvas_center, _cookies
from tests.fixtures.images import synthetic_friction, synthetic_topography, write_tiff

pytestmark = pytest.mark.browser

SIZE = 64


def _prepare(base_url: str, cookies: dict, tmp_path: Path, channel_count: int) -> tuple[str, str]:
    headers = {"X-Requested-With": "AFMLabel"}
    with httpx.Client(base_url=base_url, cookies=cookies, headers=headers, timeout=30) as client:
        project = client.post(
            "/api/v1/projects", json={"name": "Channels", "channel_count": channel_count}
        ).json()
        pid = project["project_id"]
        rev = project["revision"]
        for name in ("Boundary", "Grain"):
            rev = client.post(
                f"/api/v1/projects/{pid}/classes", json={"expected_revision": rev, "name": name}
            ).json()["project"]["revision"]
        batch = client.post(f"/api/v1/projects/{pid}/imports/pairs/batches").json()["batch_id"]
        preview = None
        for index, channel in enumerate(project["channels"]):
            token = channel["filename_tokens"][0]
            array = (
                synthetic_topography(SIZE, SIZE, seed=index)
                if index % 2 == 0
                else synthetic_friction(SIZE, SIZE, seed=index)
            )
            path = write_tiff(tmp_path / f"901_{token}.tiff", array)
            with path.open("rb") as handle:
                preview = client.post(
                    f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/files?channel={channel['key']}",
                    files=[("files", (path.name, handle, "image/tiff"))],
                ).json()
        assert preview is not None
        row = preview["rows"][0]
        assert row["status"] == "ok", row
        result = client.post(
            f"/api/v1/projects/{pid}/imports/pairs/batches/{batch}/commit",
            json={
                "expected_revision": rev,
                "pairs": [
                    {
                        "pairing_key": row["pairing_key"],
                        "files": {k: v["file_id"] for k, v in row["files"].items()},
                    }
                ],
            },
        ).json()
        return pid, result["datasets"][0]["dataset_id"]


def _ring_region(base_url: str, cookies: dict, pid: str, dsid: str) -> None:
    """A Boundary-class ring: outer 40x40 square minus an inner 20x20 hole,
    so the hole is an enclosed unlabelled area with a 1-px gap on one side."""
    headers = {"X-Requested-With": "AFMLabel"}
    mask = np.zeros((SIZE, SIZE), dtype=bool)
    mask[12:52, 12:52] = True
    mask[22:42, 22:42] = False
    mask[31, 12:22] = False  # a 1-pixel wide gap through the ring's left wall
    with httpx.Client(base_url=base_url, cookies=cookies, headers=headers, timeout=30) as client:
        classes = client.get(f"/api/v1/projects/{pid}/classes").json()["classes"]
        boundary = next(c["class_id"] for c in classes if c["name"] == "Boundary")
        created = client.post(
            f"/api/v1/projects/{pid}/datasets/{dsid}/regions",
            json={
                "expected_revision": 1,
                "class_id": boundary,
                "mask": {"format": "rle_rows", "size": [SIZE, SIZE], "counts": encode_rows(mask)},
            },
        )
        assert created.status_code == 201, created.text


def test_three_channel_navigator(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare(live_server_url, _cookies(page), tmp_path, channel_count=3)
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    page.wait_for_selector("#annotation-body[data-ready='1']")
    nav = page.locator("#channel-switch")
    expect(nav).to_have_attribute("data-channel-count", "3")
    expect(page.locator("#channel-index")).to_have_value("1")
    expect(page.locator("#channel-prev-button")).to_be_disabled()

    page.click("#channel-next-button")
    expect(page.locator("#channel-index")).to_have_value("2")
    assert page.evaluate("window.__afmlabelWorkspace.channel()") == "friction"

    page.locator("#annotation-canvas").focus()
    page.keyboard.press("3")
    expect(page.locator("#channel-index")).to_have_value("3")
    assert page.evaluate("window.__afmlabelWorkspace.channel()") == "channel_3"
    expect(page.locator("#channel-next-button")).to_be_disabled()
    expect(page.locator("#channel-select")).to_have_value("channel_3")

    page.keyboard.press("Tab")  # wraps around to the first channel
    expect(page.locator("#channel-index")).to_have_value("1")
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#channel-index")).to_have_value("3")

    page.select_option("#channel-select", "friction")
    expect(page.locator("#channel-slider")).to_have_value("2")

    # The channel is part of the persisted UI state and comes back on reload.
    page.wait_for_timeout(1200)
    page.reload()
    page.wait_for_selector("#annotation-body[data-ready='1']")
    expect(page.locator("#channel-index")).to_have_value("2")


def test_fill_tool_fills_enclosed_gap_and_fill_all(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare(live_server_url, _cookies(page), tmp_path, channel_count=2)
    _ring_region(live_server_url, _cookies(page), pid, dsid)
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    page.wait_for_selector("#annotation-body[data-ready='1']")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(1)

    # Active class: Grain (second class).
    grain = page.locator("#active-class-select option", has_text="Grain").get_attribute("value")
    page.select_option("#active-class-select", grain)
    page.keyboard.press("Escape")
    page.locator("#annotation-canvas").focus()
    page.keyboard.press("g")
    expect(page.locator('.tool-button[data-tool="fill"]')).to_have_class("tool-button active")
    expect(page.locator("#fill-options")).to_be_visible()
    page.fill("#fill-gap", "1")
    page.fill("#fill-name", "grain 1")
    page.locator("#annotation-canvas").focus()

    # Click the centre of the hole (image pixel 32, 32).
    cx, cy = _canvas_center(page)
    scale = page.evaluate(
        "Number(document.getElementById('zoom-value').textContent.replace(' %', '')) / 100"
    )
    # The canvas is fitted: image centre == canvas centre.
    page.mouse.move(cx, cy)
    page.wait_for_timeout(300)
    preview_area = page.evaluate("window.__afmlabelWorkspace.fillPreviewArea()")
    assert 0 < preview_area <= 20 * 20 + 2 * 10, (
        preview_area
    )  # hole plus the closed gap pixels only
    page.mouse.click(cx, cy)
    expect(page.locator("#save-state")).to_have_text("Saved")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    expect(page.locator("#region-list .lv-region-item.selected")).to_contain_text("grain 1")

    # Undo removes the filled region; redo restores it.
    page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(1)
    page.keyboard.press("ControlOrMeta+y")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)

    # One dialog handler for the rest of the test: dismiss or accept on demand.
    dialogs = {"accept": False, "seen": []}

    def on_dialog(dialog):  # type: ignore[no-untyped-def]
        dialogs["seen"].append(dialog.message)
        if dialogs["accept"]:
            dialog.accept()
        else:
            dialog.dismiss()

    page.on("dialog", on_dialog)

    # The hole is labelled now; clicking it does nothing but explain.
    page.mouse.click(cx, cy)
    page.wait_for_timeout(300)
    assert "unlabelled pixel" in page.locator("#action-message").text_content()
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)

    # The outside area touches the border and covers most of the image:
    # the tool asks for confirmation before creating it. Decline it.
    page.fill("#fill-gap", "0")
    page.locator("#annotation-canvas").focus()
    page.mouse.click(cx - 27 * scale, cy - 27 * scale)  # image pixel (5, 5)
    page.wait_for_timeout(400)
    assert dialogs["seen"] and "probably not enclosed" in dialogs["seen"][-1]
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)

    # Fill all enclosed gaps: nothing enclosed is left (the hole is filled),
    # the outside touches the border and is skipped by default.
    page.fill("#fill-gap", "1")
    dialogs["accept"] = True
    seen_before = len(dialogs["seen"])
    page.click("#fill-all-button")
    page.wait_for_timeout(400)
    assert len(dialogs["seen"]) == seen_before  # no confirmation: nothing to fill
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    assert "touches the image border" in page.locator("#action-message").text_content()

    # Including the border area fills the rest in one transaction.
    page.check("#fill-include-border")
    page.click("#fill-all-button")
    expect(page.locator("#save-state")).to_have_text("Saved")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(3)
    assert "Fill 1 enclosed unlabelled area" in dialogs["seen"][-1]
    expect(page.locator("#status-missing")).to_contain_text("0 px")
    page.keyboard.press("Escape")
    page.locator("#annotation-canvas").focus()
    page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
