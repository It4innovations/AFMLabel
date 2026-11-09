# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser test: validate in the workspace, then export from the project page."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_overlaps import _rows
from tests.browser.test_workspace import _cookies, _prepare_project

pytestmark = pytest.mark.browser


def test_validate_and_export_download(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    headers = {"X-Requested-With": "AFMLabel"}
    with httpx.Client(
        base_url=live_server_url, cookies=_cookies(page), headers=headers, timeout=30
    ) as client:
        classes = client.get(f"/api/v1/projects/{pid}/classes").json()["classes"]
        client.post(
            f"/api/v1/projects/{pid}/datasets/{dsid}/regions",
            json={
                "expected_revision": 1,
                "class_id": classes[0]["class_id"],
                "mask": _rows(64, 64, 0, 64, 0, 64),
            },
        )

    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    page.click("#validate-button")
    expect(page.locator("#validation-report")).to_contain_text("VALID")

    page.goto(f"{live_server_url}/projects/{pid}")
    page.click("#open-export-button")
    expect(page.locator("#export-modal")).to_be_visible()
    expect(page.locator("#export-dataset-list input")).to_be_checked()
    page.select_option("#export-profile", "nanounet")
    page.click("#export-run-button")
    expect(page.locator("#export-status")).to_contain_text("1 dataset(s) exported")
    expect(page.locator("#export-history li")).to_have_count(1)
    with page.expect_download() as download_info:
        page.click("#export-history a.lv-btn")
    download = download_info.value
    assert download.suggested_filename.endswith(".zip")
    target = tmp_path / download.suggested_filename
    download.save_as(target)
    assert target.stat().st_size > 0
