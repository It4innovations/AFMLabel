# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for region editing correctness:
brush over another class, active-class colour, locked regions,
multi-selection, Select All, Delete Selected, Merge/Split undo, Subtract by
Order and Clear selection."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_overlaps import _rows
from tests.browser.test_workspace import _canvas_center, _cookies, _prepare_project

pytestmark = pytest.mark.browser

HEADERS = {"X-Requested-With": "AFMLabel"}


def _seed(
    page: Page, base_url: str, pid: str, dsid: str, masks: list[tuple[int, tuple]]
) -> list[str]:
    """Create regions through the API: [(class_index, (y0, y1, x0, x1)), ...]."""
    ids: list[str] = []
    with httpx.Client(
        base_url=base_url, cookies=_cookies(page), headers=HEADERS, timeout=30
    ) as client:
        base = f"/api/v1/projects/{pid}/datasets/{dsid}"
        classes = client.get(f"/api/v1/projects/{pid}/classes").json()["classes"]
        for class_index, (y0, y1, x0, x1) in masks:
            revision = client.get(base).json()["dataset"]["revision"]
            created = client.post(
                f"{base}/regions",
                json={
                    "expected_revision": revision,
                    "class_id": classes[class_index]["class_id"],
                    "mask": _rows(64, 64, y0, y1, x0, x1),
                },
            ).json()
            ids.append(created["region"]["region_id"])
    return ids


def _regions(page: Page, base_url: str, pid: str, dsid: str) -> list[dict]:
    with httpx.Client(
        base_url=base_url, cookies=_cookies(page), headers=HEADERS, timeout=30
    ) as client:
        return client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/regions").json()["regions"]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = int(color.lstrip("#"), 16)
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def test_brush_creates_class_b_region_over_class_a_and_reports_overlap(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    """Regression: Class A painted, Region A selected, active class
    switched to B, painting across A creates a B region, A stays intact,
    an overlap Finding appears and the canvas shows the B colour."""
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    (region_a,) = _seed(page, live_server_url, pid, dsid, [(0, (0, 64, 0, 64))])
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(1)

    # Select Region A, switch the active class to Layer B (second class).
    page.click("#region-list .lv-region-item >> nth=0 >> .lv-region-name")
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(1)
    classes = page.locator("#active-class-select option")
    expect(classes).to_have_count(2)
    class_b = classes.nth(1).get_attribute("value")
    page.select_option("#active-class-select", class_b)
    assert page.evaluate("window.__afmlabelWorkspace.activeClassId()") == int(class_b)

    # Full opacity so a region pixel shows the pure class colour.
    page.evaluate(
        "document.getElementById('mask-opacity').value = 100; document.getElementById('mask-opacity').dispatchEvent(new Event('input'))"  # noqa: E501
    )

    page.keyboard.press("b")
    cx, cy = _canvas_center(page)
    page.mouse.move(cx - 40, cy)
    page.mouse.down()
    page.mouse.move(cx + 40, cy, steps=10)
    page.mouse.up()
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    expect(page.locator("#save-state")).to_have_text("Saved")
    regions = _regions(page, live_server_url, pid, dsid)
    by_id = {r["region_id"]: r for r in regions}
    assert by_id[region_a]["area"] == 64 * 64, "Region A must stay intact underneath"
    new = [r for r in regions if r["region_id"] != region_a][0]
    assert new["class_id"] == int(class_b) and new["area"] > 0
    assert page.evaluate("window.__afmlabelWorkspace.selectedId()") == new["region_id"]
    expect(page.locator("#status-overlaps")).to_contain_text("1 unreviewed")
    expect(page.locator("#action-message")).to_contain_text("created over")

    # The canvas pixel at the image centre is the Class B colour, not Class A.
    color_b = page.evaluate(
        f"(document.querySelector('#active-class-select option[value=\"{class_b}\"]'), null)"
    )
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=HEADERS, timeout=30
    ) as client:
        ontology = client.get(f"/api/v1/projects/{pid}/classes").json()["classes"]
    color_b = _hex_to_rgb(
        next(c["display_color"] for c in ontology if c["class_id"] == int(class_b))
    )
    pixel = page.evaluate(
        "window.__afmlabelWorkspace.samplePixel(33, 32)"
    )  # (33+32)&3 != 0: not on the overlap hatch
    assert tuple(pixel[:3]) == color_b, f"expected class B colour {color_b}, got {pixel}"


def test_locked_region_visuals_brush_over_and_eraser_refused(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    (region_a,) = _seed(page, live_server_url, pid, dsid, [(0, (0, 64, 0, 64))])
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    row = page.locator("#region-list .lv-region-item").first
    row.locator('[data-action="locked"]').click()
    expect(page.locator("#save-state")).to_have_text("Saved")
    expect(row).to_have_class(re.compile(r"locked-region"))
    expect(row.locator(".lv-region-lock-badge")).to_be_visible()
    expect(row.locator(".lv-region-lock-badge")).to_have_attribute(
        "aria-label", "Locked region — editing disabled"
    )
    expect(row.locator('[data-action="delete"]')).to_be_disabled()
    expect(row.locator("select")).to_be_disabled()
    # Tooltip of the lock badge on keyboard focus explains the state.
    row.locator(".lv-region-lock-badge").focus()
    expect(page.locator("#lv-tooltip")).to_be_visible()
    expect(page.locator("#lv-tooltip")).to_contain_text("editing disabled")

    # Selected + locked: eraser refuses, brush creates a new region over it.
    row.locator(".lv-region-name").click()
    page.keyboard.press("e")
    cx, cy = _canvas_center(page)
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.mouse.move(cx + 20, cy, steps=4)
    page.mouse.up()
    expect(page.locator("#action-message")).to_contain_text("locked")
    assert _regions(page, live_server_url, pid, dsid)[0]["area"] == 64 * 64
    page.keyboard.press("b")
    page.mouse.move(cx - 30, cy)
    page.mouse.down()
    page.mouse.move(cx + 30, cy, steps=6)
    page.mouse.up()
    expect(page.locator("#region-list .lv-region-item")).to_have_count(2)
    regions = _regions(page, live_server_url, pid, dsid)
    assert {r["region_id"]: r["area"] for r in regions}[region_a] == 64 * 64
    assert any(r["locked"] for r in regions) and sum(1 for r in regions if not r["locked"]) == 1
    # Delete Selected with a locked region in the selection keeps it.
    page.click("#region-select-all-button")
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(2)
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#region-delete-selected-button")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(1)
    expect(page.locator("#region-list .lv-region-item").first).to_have_class(
        re.compile(r"locked-region")
    )


def test_multi_selection_delete_selected_and_clear(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    ids = _seed(
        page,
        live_server_url,
        pid,
        dsid,
        [(0, (0, 10, 0, 10)), (0, (20, 30, 20, 30)), (1, (40, 50, 40, 50)), (1, (50, 60, 0, 10))],
    )
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    rows = page.locator("#region-list .lv-region-item")
    expect(rows).to_have_count(4)
    # Displayed top first: ids[3], ids[2], ids[1], ids[0].
    rows.nth(0).locator(".lv-region-name").click()
    rows.nth(2).locator(".lv-region-name").click(modifiers=["ControlOrMeta"])
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(2)
    assert set(page.evaluate("window.__afmlabelWorkspace.selectedIds()")) == {ids[3], ids[1]}
    rows.nth(2).locator(".lv-region-name").click(modifiers=["ControlOrMeta"])  # toggle off
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(1)
    rows.nth(3).locator(".lv-region-name").click(modifiers=["Shift"])  # range from anchor
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(4)
    expect(page.locator("#region-selected-count")).to_have_text("4 selected")
    page.keyboard.press("Escape")
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(0)
    expect(page.locator("#region-delete-selected-button")).to_be_disabled()

    # Ctrl+A with the Regions panel focused, then Delete Selected (one undo).
    page.click("#region-list")
    page.keyboard.press("ControlOrMeta+a")
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(4)
    expect(page.locator("#region-delete-selected-button")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.click("#region-delete-selected-button")
    expect(rows).to_have_count(0)
    expect(page.locator("#save-state")).to_have_text("Saved")
    expect(page.locator("#status-validation")).to_contain_text("not run")
    page.keyboard.press("ControlOrMeta+z")
    expect(rows).to_have_count(4)
    expect(page.locator("#save-state")).to_have_text("Saved")
    assert sorted(r["region_id"] for r in _regions(page, live_server_url, pid, dsid)) == sorted(ids)
    page.keyboard.press("ControlOrMeta+y")
    expect(rows).to_have_count(0)

    # Clear selection button exists in the toolbar and is harmless.
    page.keyboard.press("ControlOrMeta+z")
    expect(rows).to_have_count(4)
    rows.nth(0).locator(".lv-region-name").click()
    page.click("#clear-selection-button")
    expect(page.locator("#region-list .lv-region-item.selected")).to_have_count(0)
    expect(rows).to_have_count(4)


def test_merge_and_split_are_undoable_with_original_ids(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    ids = _seed(
        page,
        live_server_url,
        pid,
        dsid,
        [(0, (0, 8, 0, 8)), (0, (40, 48, 40, 48)), (1, (20, 24, 20, 24))],
    )
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    rows = page.locator("#region-list .lv-region-item")
    expect(rows).to_have_count(3)
    before = _regions(page, live_server_url, pid, dsid)

    # Merge refuses different classes with an explanation.
    rows.nth(0).locator(".lv-region-name").click()  # ids[2] (class B)
    rows.nth(1).locator(".lv-region-name").click(modifiers=["ControlOrMeta"])  # ids[1] (class A)
    page.click("#region-merge-button")
    expect(page.locator("#action-message")).to_contain_text("different classes")
    expect(rows).to_have_count(3)

    # Merge the two class-A regions, undo restores both ids and masks.
    page.keyboard.press("Escape")
    rows.nth(1).locator(".lv-region-name").click()
    rows.nth(2).locator(".lv-region-name").click(modifiers=["ControlOrMeta"])
    page.click("#region-merge-button")
    expect(rows).to_have_count(2)
    expect(page.locator("#save-state")).to_have_text("Saved")
    merged = _regions(page, live_server_url, pid, dsid)
    assert {r["region_id"] for r in merged} == {ids[0], ids[2]} or {
        r["region_id"] for r in merged
    } == {ids[1], ids[2]}
    page.keyboard.press("ControlOrMeta+z")
    expect(rows).to_have_count(3)
    expect(page.locator("#save-state")).to_have_text("Saved")
    restored = _regions(page, live_server_url, pid, dsid)
    assert {(r["region_id"], r["area"], r["class_id"], r["z_order"]) for r in restored} == {
        (r["region_id"], r["area"], r["class_id"], r["z_order"]) for r in before
    }
    page.keyboard.press("ControlOrMeta+y")
    expect(rows).to_have_count(2)
    page.keyboard.press("ControlOrMeta+z")
    expect(rows).to_have_count(3)

    # Split the merged-like two-component region: create it via merge first.
    rows.nth(1).locator(".lv-region-name").click()
    rows.nth(2).locator(".lv-region-name").click(modifiers=["ControlOrMeta"])
    page.click("#region-merge-button")
    expect(rows).to_have_count(2)
    merged_id = [
        r["region_id"]
        for r in _regions(page, live_server_url, pid, dsid)
        if r["class_id"] == before[0]["class_id"]
    ][0]
    page.locator(
        f'#region-list .lv-region-item[data-region-id="{merged_id}"] .lv-region-name'
    ).click()
    page.click("#region-split-button")
    expect(rows).to_have_count(3)
    expect(page.locator("#action-message")).to_contain_text("Split into 2")
    page.keyboard.press("ControlOrMeta+z")
    expect(rows).to_have_count(2)
    after_undo = _regions(page, live_server_url, pid, dsid)
    assert {r["region_id"] for r in after_undo} == {merged_id, ids[2]}
    assert next(r for r in after_undo if r["region_id"] == merged_id)["area"] == 128


def test_subtract_by_order_resolves_overlap_and_is_undoable(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    ids = _seed(
        page,
        live_server_url,
        pid,
        dsid,
        [(0, (0, 64, 0, 64)), (1, (10, 30, 10, 30)), (1, (50, 60, 50, 60)), (0, (48, 62, 48, 62))],
    )
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    rows = page.locator("#region-list .lv-region-item")
    expect(rows).to_have_count(4)
    expect(page.locator("#status-overlaps")).to_contain_text("2 unreviewed")
    expect(page.locator("#region-subtract-button")).to_be_disabled()
    # Select the big region (bottom) and the first island.
    rows.nth(3).locator(".lv-region-name").click()
    rows.nth(2).locator(".lv-region-name").click(modifiers=["ControlOrMeta"])
    expect(page.locator("#region-subtract-button")).to_be_enabled()
    dialogs: list[str] = []
    page.once("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    page.click("#region-subtract-button")
    expect(page.locator("#action-message")).to_contain_text("Subtracted 400 px")
    assert "keeps its pixels" in dialogs[0] and ids[0] in dialogs[0]
    regions = {r["region_id"]: r for r in _regions(page, live_server_url, pid, dsid)}
    assert regions[ids[0]]["area"] == 64 * 64 - 400
    assert regions[ids[1]]["area"] == 400
    assert regions[ids[2]]["area"] == 100 and regions[ids[3]]["area"] == 196  # not selected
    # The remaining overlap group is the untouched island / cover pair.
    expect(page.locator("#status-overlaps")).to_contain_text("Overlaps: 1")
    page.keyboard.press("ControlOrMeta+z")
    expect(page.locator("#save-state")).to_have_text("Saved")
    regions = {r["region_id"]: r for r in _regions(page, live_server_url, pid, dsid)}
    assert regions[ids[0]]["area"] == 64 * 64
    expect(page.locator("#status-overlaps")).to_contain_text("2 unreviewed")

    # Fully covered region: confirmation names it, it is removed, undo restores it.
    page.keyboard.press("Escape")
    rows.nth(0).locator(".lv-region-name").click()  # cover region (top)
    page.locator(f'#region-list .lv-region-item[data-region-id="{ids[2]}"] .lv-region-name').click(
        modifiers=["ControlOrMeta"]
    )
    dialogs.clear()
    page.once("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
    page.click("#region-subtract-button")
    expect(rows).to_have_count(3)
    assert "become empty" in dialogs[0]
    assert ids[2] not in {r["region_id"] for r in _regions(page, live_server_url, pid, dsid)}
    page.keyboard.press("ControlOrMeta+z")
    expect(rows).to_have_count(4)
    assert ids[2] in {r["region_id"] for r in _regions(page, live_server_url, pid, dsid)}
