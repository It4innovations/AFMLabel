# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for the Findings panel: default
sorting, filters, multi-selection, bulk review, Validate from the panel,
reviewed Findings sinking below unreviewed ones, stale banner."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_regions_editing import _regions, _seed
from tests.browser.test_workspace import _cookies, _prepare_project

pytestmark = pytest.mark.browser


def test_findings_sorting_filters_multiselect_and_bulk_review(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    # Base covers the top half only -> missing coverage; three islands overlap it.
    _seed(
        page,
        live_server_url,
        pid,
        dsid,
        [(0, (0, 40, 0, 64)), (1, (0, 8, 0, 8)), (1, (10, 18, 10, 18)), (1, (20, 28, 20, 28))],
    )
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    page.click("#tab-findings")
    items = page.locator("#findings-list .lv-finding-item[data-finding-id]")
    expect(items).to_have_count(4)
    types = [items.nth(i).get_attribute("data-finding-type") for i in range(4)]
    # Blocking unreviewed first: missing coverage and overlaps are both
    # blocking here (review required); deterministic type order inside.
    assert types == ["missing_coverage", "overlap", "overlap", "overlap"]
    expect(page.locator("#findings-count")).to_have_text("4")
    expect(page.locator("#findings-filter-type option")).to_have_count(3)

    # Filter by type and severity.
    page.select_option("#findings-filter-type", "overlap")
    expect(items).to_have_count(3)
    page.select_option("#findings-filter-severity", "warning")
    expect(items).to_have_count(0)
    page.select_option("#findings-filter-severity", "all")
    page.select_option("#findings-filter-type", "all")

    # Multi-select: click, Ctrl+click, Shift+click, Ctrl+A when focused.
    items.nth(1).click()
    items.nth(3).click(modifiers=["Shift"])
    expect(page.locator("#findings-list .lv-finding-item.selected")).to_have_count(3)
    expect(page.locator("#findings-selected-count")).to_have_text("3 selected")
    items.nth(2).click(modifiers=["ControlOrMeta"])  # toggle one off
    expect(page.locator("#findings-list .lv-finding-item.selected")).to_have_count(2)
    page.keyboard.press("Escape")
    expect(page.locator("#findings-list .lv-finding-item.selected")).to_have_count(0)
    page.click("#findings-list")
    page.keyboard.press("ControlOrMeta+a")
    expect(page.locator("#findings-list .lv-finding-item.selected")).to_have_count(4)

    # Mark Selected Reviewed only touches overlaps; geometry is unchanged.
    areas_before = sorted(r["area"] for r in _regions(page, live_server_url, pid, dsid))
    page.click("#findings-mark-selected-button")
    expect(page.locator("#status-overlaps")).not_to_contain_text("unreviewed")
    assert sorted(r["area"] for r in _regions(page, live_server_url, pid, dsid)) == areas_before
    types = [items.nth(i).get_attribute("data-finding-type") for i in range(4)]
    assert types[0] == "missing_coverage"  # still blocking, first
    for i in range(1, 4):
        expect(items.nth(i)).to_have_class(re.compile(r"review-reviewed"))
    page.select_option("#findings-filter-status", "unreviewed")
    expect(items).to_have_count(1)
    page.select_option("#findings-filter-status", "reviewed")
    expect(items).to_have_count(3)
    page.select_option("#findings-filter-status", "all")

    # Reset one review, it moves above the reviewed ones again.
    page.locator('#findings-list button[data-state="unreviewed"]').first.click()
    expect(page.locator("#status-overlaps")).to_contain_text("1 unreviewed")
    types_and_states = [
        (
            items.nth(i).get_attribute("data-finding-type"),
            "review-reviewed" in (items.nth(i).get_attribute("class") or ""),
        )
        for i in range(4)
    ]
    assert types_and_states[1] == ("overlap", False)
    assert all(state for _, state in types_and_states[2:])

    # Validate directly from the panel; blocked by the missing pixels.
    page.click("#findings-validate-button")
    expect(page.locator("#validation-report")).to_contain_text("BLOCKED")
    expect(page.locator("#validation-report")).to_contain_text("UNLABELED_PIXELS")
    expect(page.locator("#status-validation")).to_contain_text("blocked")
    # Blocking non-overlap Findings cannot be waived by review: the button
    # stays disabled when only the missing-coverage item is selected.
    page.keyboard.press("Escape")
    items.nth(0).click()
    expect(page.locator("#findings-mark-selected-button")).to_be_disabled()

    # Keyboard shortcut R marks the selected overlap reviewed.
    items.nth(1).click()
    page.keyboard.press("r")
    expect(page.locator("#status-overlaps")).not_to_contain_text("unreviewed")

    # Stale banner after a mutation, and the stored report is marked stale.
    page.click("#tab-regions")
    page.locator("#region-list .lv-region-item").first.locator('[data-action="visible"]').click()
    expect(page.locator("#save-state")).to_have_text("Saved")
    page.click("#tab-findings")
    expect(page.locator("#findings-stale")).to_be_visible()
    expect(page.locator("#findings-stale")).to_contain_text("stale")
    expect(page.locator("#status-validation")).to_contain_text("stale")
