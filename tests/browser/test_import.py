# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Browser test: batch topography/friction upload, pairing preview, commit."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.fixtures.images import write_pair

pytestmark = pytest.mark.browser


def test_batch_import_with_pairing_preview(
    logged_in_page: Page, live_server_url: str, tmp_path: Path
) -> None:
    page = logged_in_page
    page.click("#create-project-button")
    page.fill("#project_name", "Import project")
    page.click("#submit-create-project-button")
    page.wait_for_url(f"{live_server_url}/projects/**")

    t1, f1 = write_pair(tmp_path, "239")
    t2, f2 = write_pair(tmp_path, "240")
    t3, _ = write_pair(tmp_path, "241")

    page.click("#import-pairs-button")
    page.set_input_files("#import-files-topography", [str(t1), str(t2), str(t3)])
    page.set_input_files("#import-files-friction", [str(f1), str(f2)])
    page.click("#import-upload-button")
    expect(page.locator("#pairing-preview-body tr")).to_have_count(3)
    expect(page.locator("#pairing-preview-body")).to_contain_text("missing Friction")
    expect(page.locator("#pairing-summary")).to_contain_text("2 dataset(s) ready, 1 problem(s)")

    page.click("#import-commit-button")
    expect(page.locator("#import-pairs-modal")).to_be_hidden()
    expect(page.locator(".lv-dataset-card")).to_have_count(2)
    expect(page.locator("#dataset-count")).to_have_text("2")
    expect(page.locator(".lv-dataset-card").first).to_contain_text("239")
