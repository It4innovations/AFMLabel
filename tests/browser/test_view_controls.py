# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for view-only Brightness/Contrast:
only the rendered source image changes, overlay colours stay, no
revision/autosave, Reset View, per-channel state, export bytes and the
SAM request payload are unaffected."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_regions_editing import HEADERS, _hex_to_rgb, _seed
from tests.browser.test_workspace import _canvas_center, _cookies, _prepare_project

pytestmark = pytest.mark.browser


def _set_slider(page: Page, element_id: str, value: int) -> None:
    page.evaluate(
        f"(() => {{ const el = document.getElementById('{element_id}'); el.value = '{value}';"
        " el.dispatchEvent(new Event('input', { bubbles: true })); })()"
    )


def _export_mask_bytes(page: Page, base_url: str, pid: str) -> bytes:
    # The open workspace tab holds the editing lock; act as that tab.
    editor = page.evaluate("window.sessionStorage.getItem('afmlabel-editor-id')") or "test"
    headers = {**HEADERS, "X-AFMLabel-Editor": editor}
    with httpx.Client(
        base_url=base_url, cookies=_cookies(page), headers=headers, timeout=60
    ) as client:
        response = client.post(f"/api/v1/projects/{pid}/exports", json={"profile_id": "nanounet"})
        assert response.status_code == 201, response.text
        record = response.json()
        package = client.get(
            f"/api/v1/projects/{pid}/exports/{record['export_id']}/download"
        ).content
    archive = zipfile.ZipFile(io.BytesIO(package))
    entry = record["datasets"][0]
    return (
        archive.read(entry["files"]["mask"])
        + archive.read(entry["files"]["topography"])
        + archive.read(entry["files"]["mask_color"])
    )


def test_brightness_contrast_are_view_only(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    _seed(page, live_server_url, pid, dsid, [(0, (0, 64, 0, 32))])  # left half covered
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=30
    ) as client:
        base = f"/api/v1/projects/{pid}/datasets/{dsid}"
        revision = client.get(base).json()["dataset"]["revision"]
        classes = client.get(f"/api/v1/projects/{pid}/classes").json()["classes"]
        client.post(
            f"{base}/regions",
            json={
                "expected_revision": revision,
                "class_id": classes[1]["class_id"],
                "mask": {
                    "format": "rle_rows",
                    "size": [64, 64],
                    "counts": [32] + [32, 32] * 63 + [32],
                },
            },
        )
        for group in client.get(f"{base}/overlaps").json()["groups"]:
            revision = client.get(base).json()["dataset"]["revision"]
            client.post(
                f"{base}/overlaps/{group['group_id']}/review",
                json={"expected_revision": revision, "state": "reviewed_resolved"},
            )
        assert client.post(f"{base}/validate").json()["status"] == "valid"
        color_a = _hex_to_rgb(classes[0]["display_color"])
    before_export = _export_mask_bytes(page, live_server_url, pid)

    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("body")).to_have_attribute("data-ready", "1")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    _set_slider(page, "mask-opacity", 100)
    revision = page.evaluate("window.__afmlabelWorkspace.revision()")
    page.wait_for_timeout(300)

    # Overlay pixel (inside region A) is the pure class colour; a source
    # pixel far from any region... every pixel is covered here, so compare
    # the *base image* through a second sample: hide overlays via opacity 0.
    overlay_before = page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)")
    assert tuple(overlay_before[:3]) == color_a
    _set_slider(page, "mask-opacity", 0)
    source_before = page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)")

    _set_slider(page, "view-brightness", 160)
    expect(page.locator("#view-brightness-value")).to_have_text("160 %")
    expect(page.locator("#view-state")).to_contain_text("view only")
    source_bright = page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)")
    assert source_bright[:3] != source_before[:3], "brightness must change the rendered source"
    _set_slider(page, "mask-opacity", 100)
    overlay_bright = page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)")
    assert tuple(overlay_bright[:3]) == color_a, "region overlay colour must not change"

    _set_slider(page, "mask-opacity", 0)
    _set_slider(page, "view-brightness", 100)
    _set_slider(page, "view-contrast", 40)
    source_contrast = page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)")
    assert source_contrast[:3] != source_before[:3], "contrast must change the rendered source"
    _set_slider(page, "mask-opacity", 100)
    assert tuple(page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)")[:3]) == color_a
    _set_slider(page, "mask-opacity", 0)

    # No revision change, no autosave, nothing persisted in ui-state.
    page.wait_for_timeout(1000)
    assert page.evaluate("window.__afmlabelWorkspace.revision()") == revision
    expect(page.locator("#save-state")).to_have_text("Saved")
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=30
    ) as client:
        ui_state = client.get(f"/api/v1/projects/{pid}/ui-state").json()
        assert "brightness" not in ui_state and "contrast" not in ui_state
        assert (
            client.get(f"/api/v1/projects/{pid}/datasets/{dsid}").json()["dataset"]["revision"]
            == revision
        )

    # Reset View restores the neutral rendering exactly.
    page.click("#view-reset-button")
    expect(page.locator("#view-brightness-value")).to_have_text("100 %")
    expect(page.locator("#view-contrast-value")).to_have_text("100 %")
    assert page.evaluate("window.__afmlabelWorkspace.samplePixel(8, 32)") == source_before
    expect(page.locator("#view-reset-button")).to_be_disabled()

    # Per-channel temporary values: friction keeps its own neutral state.
    _set_slider(page, "view-brightness", 150)
    page.keyboard.press("2")
    expect(page.locator("#view-brightness-value")).to_have_text("100 %")
    page.keyboard.press("1")
    expect(page.locator("#view-brightness-value")).to_have_text("150 %")
    assert page.evaluate("window.__afmlabelWorkspace.view()") == {
        "brightness": 150,
        "contrast": 100,
    }

    # Export is byte-identical while the view is adjusted.
    assert _export_mask_bytes(page, live_server_url, pid) == before_export

    # SAM request payload carries no view adjustment.
    page.select_option("#backend-select", "fake")
    expect(page.locator('.tool-button[data-tool="sam_positive"]')).to_be_enabled()
    page.keyboard.press("q")
    cx, cy = _canvas_center(page)
    with page.expect_request(
        lambda r: r.url.endswith("/segment") and r.method == "POST"
    ) as request_info:
        page.mouse.click(cx, cy)
    payload = request_info.value.post_data_json
    assert set(payload) <= {
        "backend_id",
        "channel",
        "points",
        "box_xyxy",
        "text_prompt",
        "previous_region_id",
    }
    assert "brightness" not in str(payload).lower() and "contrast" not in str(payload).lower()
    page.click("#proposal-reject-button")

    # A reload resets the temporary view state.
    page.reload()
    expect(page.locator("#view-brightness-value")).to_have_text("100 %")
