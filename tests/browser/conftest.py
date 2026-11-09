# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import Page

from afmlabel.auth.account import AccountStore
from afmlabel.main import create_app
from afmlabel.settings import AppSettings
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


@pytest.fixture
def live_server_settings(tmp_path: Path) -> AppSettings:
    settings = AppSettings(
        workspace={"root": tmp_path / "workspace"}, logging={"json_output": False}
    )
    AccountStore(settings.workspace.root).set_password(TEST_USERNAME, TEST_PASSWORD)
    return settings


@pytest.fixture
def live_server_url(live_server_settings: AppSettings) -> Iterator[str]:
    app = create_app(live_server_settings)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", log_config=None)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn server did not start in time")

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def browser_login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill("#username", TEST_USERNAME)
    page.fill("#password", TEST_PASSWORD)
    page.click("#login-button")
    page.wait_for_url(f"{base_url}/")


@pytest.fixture
def logged_in_page(page: Page, live_server_url: str) -> Page:
    browser_login(page, live_server_url)
    return page
