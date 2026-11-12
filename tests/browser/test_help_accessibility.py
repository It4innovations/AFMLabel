# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser tests for contextual help: accessible names,
tooltips on hover and keyboard focus, required wording, legibility in both
themes, local Help links, and the coverage audit."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_regions_editing import _seed
from tests.browser.test_workspace import _cookies, _prepare_project

pytestmark = pytest.mark.browser


def test_annotation_controls_have_names_tooltips_and_local_help(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    # Two overlapping regions so the structural buttons and the review
    # button are enabled (disabled buttons cannot take keyboard focus).
    _seed(page, live_server_url, pid, dsid, [(0, (0, 64, 0, 64)), (1, (10, 20, 10, 20))])
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("body")).to_have_attribute("data-ready", "1")
    page.click("#region-select-all-button")
    expect(page.locator("#region-subtract-button")).to_be_enabled()

    # 1. every icon-only toolbar control has an accessible name.
    for button in page.locator("#toolbar button").all():
        assert (button.get_attribute("aria-label") or "").strip(), (
            "toolbar button without accessible name"
        )

    # 2. + 3. tooltip on hover and on keyboard focus, from the registry.
    brush = page.locator('.tool-button[data-tool="brush"]')
    brush.hover()
    tooltip = page.locator("#lv-tooltip")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("creates a new region of the active class")
    page.mouse.move(0, 0)
    expect(tooltip).to_be_hidden()
    page.locator("#region-subtract-button").focus()
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("higher-priority")
    assert page.locator("#region-subtract-button").get_attribute("aria-describedby") == "lv-tooltip"
    # Native title attributes are not the only source: they were removed.
    assert brush.get_attribute("title") is None

    # 4. destructive actions explain their effect.
    page.locator("#region-delete-selected-button").hover()
    expect(tooltip).to_contain_text("confirmation")
    expect(tooltip).to_contain_text("Locked regions are kept")
    # 5. + 6. brightness / contrast say they are view only.
    page.locator("#view-brightness").hover()
    expect(tooltip).to_contain_text("Change only the displayed source-image brightness")
    expect(tooltip).to_contain_text("not saved")
    page.locator("#view-contrast").hover()
    expect(tooltip).to_contain_text("Change only the displayed source-image contrast")
    # 7. lock help explains edit prevention.
    page.locator("#region-merge-button").hover()
    expect(tooltip).to_contain_text("same class")
    # 8. review vs geometry.
    page.click("#tab-findings")
    page.locator('#findings-list .lv-finding-item[data-finding-type="overlap"]').first.click()
    expect(page.locator("#findings-mark-selected-button")).to_be_enabled()
    page.locator("#findings-mark-selected-button").hover()
    expect(tooltip).to_contain_text("never subtracts")

    # 10. legible in both themes (text colour differs from background).
    for theme in ("dark", "light"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        page.locator("#findings-validate-button").hover()
        expect(tooltip).to_be_visible()
        colors = page.evaluate(
            "(() => { const s = getComputedStyle(document.getElementById('lv-tooltip')); return [s.color, s.backgroundColor]; })()"  # noqa: E501
        )
        assert colors[0] != colors[1]
    page.evaluate("document.documentElement.dataset.theme = 'dark'")

    # Extended help popover with a link to the local Help; Esc closes it
    # without touching the selection.
    page.click("#tab-regions")
    page.click('[data-help-more="regions.subtract"]')
    popover = page.locator("#lv-help-popover")
    expect(popover).to_be_visible()
    link = popover.locator("a")
    href = link.get_attribute("href") or ""
    assert href.startswith("/help/user-guide#")
    page.keyboard.press("Escape")
    expect(popover).to_be_hidden()

    # 11. + 12. Help links resolve locally (no remote service).
    response = page.request.get(f"{live_server_url}{href.split('#')[0]}")
    assert response.ok and "Subtract by Order" in response.text()
    index = page.request.get(f"{live_server_url}/help")
    assert index.ok and "User guide" in index.text()

    # Coverage audit: no interactive element without name or help key.
    problems = page.evaluate("window.__afmlabelHelp.audit()")
    assert problems == [], problems


def test_upload_and_preview_pairing_is_an_obvious_button_in_both_themes(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, _ = _prepare_project(live_server_url, _cookies(page), tmp_path)
    page.goto(f"{live_server_url}/projects/{pid}")
    page.click("#import-pairs-button")
    button = page.locator("#import-upload-button")
    expect(button).to_be_visible()
    assert button.evaluate("el => el.tagName") == "BUTTON"
    assert "lv-btn-primary" in (button.get_attribute("class") or "")
    for theme in ("dark", "light"):
        page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        styles = button.evaluate(
            "el => { const s = getComputedStyle(el); const p = getComputedStyle(el.parentElement); return { bg: s.backgroundColor, parent: p.backgroundColor, border: s.borderTopWidth, cursor: s.cursor, weight: s.fontWeight }; }"  # noqa: E501
        )
        assert styles["bg"] != styles["parent"], f"{theme}: button must have a distinct fill"
        assert styles["bg"] not in ("rgba(0, 0, 0, 0)", "transparent")
        assert styles["border"] != "0px" and styles["cursor"] == "pointer"
        assert int(styles["weight"]) >= 600
    # Keyboard reachable: Tab from the last file input lands on it.
    page.locator("#import-auto-files").focus()
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement.id") == "import-upload-button"
    # Focus ring visible.
    ring = button.evaluate("el => getComputedStyle(el).boxShadow")
    assert ring and ring != "none"
    page.keyboard.press("Enter")
    expect(page.locator("#import-error")).to_contain_text(
        "Select files for the channels first."
    )


def test_help_user_guide_shows_its_screenshots(logged_in_page: Page, live_server_url: str) -> None:
    page = logged_in_page
    page.goto(f"{live_server_url}/help/user-guide")
    figures = page.locator("#help-article p.lv-help-figure img")
    expect(figures.first).to_be_visible()
    assert figures.count() >= 10
    # Every image loads from the local Help route; none is broken.
    page.evaluate(
        "Promise.all([...document.querySelectorAll('#help-article img')]"
        ".map((img) => { img.loading = 'eager'; return img.decode(); }))"
    )
    broken = page.evaluate(
        "[...document.querySelectorAll('#help-article img')]"
        ".filter((img) => !img.complete || img.naturalWidth === 0).map((img) => img.src)"
    )
    assert broken == []
    sources = figures.evaluate_all("(imgs) => imgs.map((img) => img.getAttribute('src'))")
    assert all(src.startswith("/help/images/") for src in sources), sources
    # The figure never overflows the article column.
    widths = page.evaluate(
        "(() => { const a = document.getElementById('help-article').clientWidth;"
        " const img = document.querySelector('#help-article img').getBoundingClientRect().width;"
        " return [a, img]; })()"
    )
    assert widths[1] <= widths[0]
