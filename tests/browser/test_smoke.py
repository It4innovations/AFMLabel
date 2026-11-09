# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Playwright smoke tests: shell, system status, theme, login/logout, projects."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.browser


def test_login_logout_and_system_status(page: Page, live_server_url: str) -> None:
    page.goto(f"{live_server_url}/")
    page.wait_for_url(f"{live_server_url}/login?**")
    page.fill("#username", "tester")
    page.fill("#password", "wrong password")
    page.click("#login-button")
    expect(page.locator("#login-error")).to_contain_text("invalid username or password")

    page.fill("#password", "correct-horse-battery")
    page.click("#login-button")
    page.wait_for_url(f"{live_server_url}/")
    expect(page.locator(".lv-logo")).to_have_text("AFMLabel")
    expect(page.locator("#current-user")).to_have_text("tester")

    page.click("#open-system-status-button")
    expect(page.locator("#system-status-modal")).to_be_visible()
    expect(page.locator("#backends-list")).to_contain_text("fake")
    expect(page.locator("#resources-list")).to_contain_text("RAM")
    page.click("#system-status-dismiss-button")

    page.click("#logout-button")
    page.wait_for_url(f"{live_server_url}/login")


def test_theme_toggle_persists(logged_in_page: Page, live_server_url: str) -> None:
    page = logged_in_page
    page.click("#theme-toggle")
    assert page.evaluate("document.documentElement.dataset.theme") == "light"
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "light"


def test_create_project_manage_classes_and_reopen(
    logged_in_page: Page, live_server_url: str
) -> None:
    page = logged_in_page
    page.click("#create-project-button")
    page.fill("#project_name", "Browser project")
    page.click("#submit-create-project-button")
    page.wait_for_url(f"{live_server_url}/projects/**")
    expect(page.locator("#project-title")).to_have_text("Browser project")
    expect(page.locator("#lock-indicator")).to_contain_text("held by this tab")

    page.fill("#new-class-name", "Layer A")
    page.click("#class-create-form button[type=submit]")
    expect(page.locator("#class-table-body tr")).to_have_count(1)
    expect(page.locator("#project-revision")).to_have_text("2")

    page.goto(f"{live_server_url}/")
    expect(page.locator(".lv-card")).to_have_count(1)
    expect(page.locator(".lv-card")).to_contain_text("1 class")
    page.click(".lv-open-button")
    expect(page.locator("#class-table-body tr")).to_have_count(1)
