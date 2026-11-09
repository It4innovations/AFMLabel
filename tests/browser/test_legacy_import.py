# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser test: TSWAT/CVAT XML import dialog with preview and commit."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.browser.test_workspace import _cookies, _prepare_project
from tests.fixtures.cvat_xml import hidden_region_fixture

pytestmark = pytest.mark.browser


def test_xml_import_preview_and_commit(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    pid, dsid = _prepare_project(live_server_url, _cookies(page), tmp_path)
    xml, _ = hidden_region_fixture(image_name="801_topography.tiff")
    xml_path = tmp_path / "annotations.xml"
    xml_path.write_text(xml, encoding="utf-8")

    page.goto(f"{live_server_url}/projects/{pid}")
    expect(page.locator(".lv-dataset-card")).to_have_count(1)
    page.click("#import-tswat-button")
    page.set_input_files("#tswat-file", str(xml_path))
    page.click("#tswat-stage-button")
    expect(page.locator("#tswat-images-body tr")).to_have_count(1)
    expect(page.locator("#tswat-images-body")).to_contain_text("matched")
    expect(page.locator("#tswat-labels-body tr")).to_have_count(3)
    expect(page.locator("#tswat-summary")).to_contain_text("3 shape(s)")

    page.click("#tswat-commit-button")
    expect(page.locator("#tswat-result")).to_contain_text("3 region(s) imported")
    expect(page.locator("#tswat-result")).to_contain_text("unreviewed")
    page.click("#cancel-tswat-modal-button")
    expect(page.locator(".lv-dataset-card").first).to_contain_text("3 regions")
    expect(page.locator("#class-table-body tr")).to_have_count(5)

    # The workspace shows both overlapping regions and the unreviewed group.
    page.goto(f"{live_server_url}/projects/{pid}/datasets/{dsid}")
    expect(page.locator("#region-list .lv-region-item")).to_have_count(3)
    expect(page.locator("#status-overlaps")).to_contain_text("1 unreviewed")
