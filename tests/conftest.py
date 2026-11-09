# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from afmlabel.auth.account import AccountStore
from afmlabel.main import create_app
from afmlabel.settings import AppSettings

FIXTURES_DIR = Path(__file__).parent / "fixtures"
#: Developer-local real data (git-ignored); tests that need it skip otherwise.
DEVELOPER_DATA_DIR = Path(__file__).resolve().parent.parent / ".data" / "extracted"

TEST_USERNAME = "tester"
TEST_PASSWORD = "correct-horse-battery"

#: Every state-changing API call must carry the same-origin marker header.
CSRF_HEADERS = {"X-Requested-With": "AFMLabel"}


@pytest.fixture
def app_settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        workspace={"root": tmp_path / "workspace"},
        logging={"json_output": False},
    )


@pytest.fixture
def configured_account(app_settings: AppSettings) -> AccountStore:
    store = AccountStore(app_settings.workspace.root)
    store.set_password(TEST_USERNAME, TEST_PASSWORD)
    return store


@pytest.fixture
def anonymous_client(app_settings: AppSettings, configured_account: AccountStore) -> TestClient:
    """Client against an app with an account but no session."""
    app = create_app(app_settings)
    client = TestClient(app)
    client.headers.update(CSRF_HEADERS)
    return client


def login(client: TestClient, username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> None:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200, response.text


@pytest.fixture
def api_client(anonymous_client: TestClient) -> TestClient:
    """Authenticated client with the CSRF header pre-set."""
    login(anonymous_client)
    return anonymous_client


def create_project(client: TestClient, name: str = "Demo project") -> dict:
    response = client.post("/api/v1/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()
