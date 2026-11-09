# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser test: every dataset-card action button is fully visible inside the
card (no clipping by ``overflow: hidden``), also at the narrowest grid column."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_workspace import _cookies, _prepare_project

pytestmark = pytest.mark.browser


def test_dataset_card_buttons_are_not_clipped(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, _ = _prepare_project(live_server_url, _cookies(page), tmp_path)
    page.set_viewport_size({"width": 620, "height": 800})
    page.goto(f"{live_server_url}/projects/{pid}")
    card = page.locator(".lv-dataset-card").first
    expect(card).to_be_visible()
    box = card.bounding_box()
    assert box is not None
    buttons = card.locator(".lv-dataset-actions .lv-btn")
    expect(buttons).to_have_count(4)
    for index in range(4):
        button = buttons.nth(index)
        expect(button).to_be_visible()
        rect = button.bounding_box()
        assert rect is not None
        assert rect["x"] >= box["x"] - 0.5
        assert rect["x"] + rect["width"] <= box["x"] + box["width"] + 0.5, (
            f"button {index} overflows the card"
        )
        assert rect["y"] + rect["height"] <= box["y"] + box["height"] + 0.5
    page.screenshot(path=str(Path("tmp") / "dataset-card-buttons.png"), clip=box)
