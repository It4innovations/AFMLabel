# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser test: SAM point prompt -> job -> proposal overlay -> accept/reject
with the fake backend, and the unavailable-backend state."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_workspace import _canvas_center, _cookies, _prepare_project

pytestmark = pytest.mark.browser


def test_proposal_accept_and_reject(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("#lock-indicator")).to_contain_text("held by this tab")

    # The fake backend advertises point prompts; the real backends are listed
    # but unavailable (no checkpoint in the test configuration).
    options = page.locator("#backend-select option")
    expect(options).to_have_count(4)
    for backend_id in ("sam3", "sam31", "sam21"):
        expect(page.locator(f'#backend-select option[value="{backend_id}"]')).to_contain_text(
            "unavailable"
        )
    page.select_option("#backend-select", "fake")
    expect(page.locator('.tool-button[data-tool="sam_positive"]')).to_be_enabled()
    # Capability-driven extras: the fake backend has mask prompts but no text.
    expect(page.locator("#prompt-refine-selected")).to_be_enabled()
    expect(page.locator("#prompt-text")).to_be_disabled()
    assert "Unavailable" in (page.locator("#prompt-text").get_attribute("data-help-note") or "")

    page.keyboard.press("q")
    cx, cy = _canvas_center(page)
    page.mouse.click(cx, cy)
    expect(page.locator("#proposal-actions")).to_be_visible()
    expect(page.locator("#proposal-hint")).to_contain_text("variant(s)")
    expect(page.locator("#proposal-select option")).to_have_count(2)
    # Nothing is committed yet.
    expect(page.locator("#region-list .lv-region-item")).to_have_count(0)

    page.click("#proposal-reject-button")
    expect(page.locator("#proposal-actions")).to_be_hidden()
    expect(page.locator("#region-list .lv-region-item")).to_have_count(0)

    page.mouse.click(cx + 40, cy + 40)
    expect(page.locator("#proposal-actions")).to_be_visible()
    expect(page.locator("#proposal-select option")).to_have_count(2)
    page.click("#proposal-accept-button")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(1)
    expect(page.locator("#region-list .lv-region-item")).to_contain_text("sam_proposal")
    expect(page.locator("#save-state")).to_have_text("Saved")

    # Mask prompt: the selected region is sent as previous_region_id.
    page.click("#region-list .lv-region-item .lv-region-name")
    page.check("#prompt-refine-selected")
    page.keyboard.press("q")
    page.mouse.click(cx + 20, cy + 20)
    expect(page.locator("#proposal-actions")).to_be_visible()
    expect(page.locator("#proposal-hint")).to_contain_text("variant(s)")
    page.click("#proposal-reject-button")
    page.uncheck("#prompt-refine-selected")

    # Selecting the unavailable backend disables the SAM tools with a reason.
    page.select_option("#backend-select", "sam31")
    expect(page.locator('.tool-button[data-tool="sam_positive"]')).to_be_disabled()
    title = (
        page.locator('.tool-button[data-tool="sam_positive"]').get_attribute("data-help-note") or ""
    )
    assert "Unavailable" in title and "disabled in configuration" in title
