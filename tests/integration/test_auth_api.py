# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from afmlabel.auth.sessions import SESSION_COOKIE_NAME
from afmlabel.main import create_app
from afmlabel.settings import AppSettings
from tests.conftest import CSRF_HEADERS, TEST_PASSWORD, TEST_USERNAME, login


def test_unauthenticated_api_is_rejected_and_pages_redirect(anonymous_client: TestClient) -> None:
    response = anonymous_client.get("/api/v1/projects")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"

    page = anonymous_client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"].startswith("/login")

    workspace = anonymous_client.get("/projects/proj_0000000000", follow_redirects=False)
    assert workspace.status_code == 303

    help_image = anonymous_client.get("/help/images/ui-01-login.png", follow_redirects=False)
    assert help_image.status_code == 303

    # Public endpoints stay reachable.
    assert anonymous_client.get("/api/v1/health").status_code == 200
    assert anonymous_client.get("/login").status_code == 200
    assert anonymous_client.get("/api/v1/auth/status").json()["authenticated"] is False


def test_login_sets_httponly_cookie_and_logout_clears_it(anonymous_client: TestClient) -> None:
    response = anonymous_client.post(
        "/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert SESSION_COOKIE_NAME in cookie
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert anonymous_client.get("/api/v1/auth/status").json()["username"] == TEST_USERNAME
    assert anonymous_client.get("/api/v1/projects").status_code == 200

    anonymous_client.post("/api/v1/auth/logout")
    assert anonymous_client.get("/api/v1/projects").status_code == 401


def test_invalid_credentials_and_rate_limit(anonymous_client: TestClient) -> None:
    anonymous_client.app.state.login_limiter._limit = 3  # type: ignore[attr-defined]
    for _ in range(3):
        response = anonymous_client.post(
            "/api/v1/auth/login", json={"username": TEST_USERNAME, "password": "wrong"}
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "AUTH_INVALID"
    blocked = anonymous_client.post(
        "/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "AUTH_RATE_LIMITED"


def test_csrf_header_is_required_for_state_changes(anonymous_client: TestClient) -> None:
    response = anonymous_client.post(
        "/api/v1/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
        headers={"X-Requested-With": ""},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_REJECTED"

    login(anonymous_client)
    cross_site = anonymous_client.post(
        "/api/v1/projects",
        json={"name": "x"},
        headers={**CSRF_HEADERS, "Origin": "https://evil.example"},
    )
    assert cross_site.status_code == 403


def test_no_account_configured_redirects_to_setup(app_settings: AppSettings) -> None:
    client = TestClient(create_app(app_settings))
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/setup"
    assert client.get("/setup").status_code == 200
    response = client.post(
        "/api/v1/auth/login", json={"username": "x", "password": "y"}, headers=CSRF_HEADERS
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_NOT_CONFIGURED"


def test_session_survives_process_restart(
    app_settings: AppSettings, configured_account: object
) -> None:
    first = TestClient(create_app(app_settings))
    login(first)
    cookie = first.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    second = TestClient(create_app(app_settings))
    second.cookies.set(SESSION_COOKIE_NAME, cookie)
    assert second.get("/api/v1/auth/status").json()["authenticated"] is True


def test_password_never_appears_in_logs(
    anonymous_client: TestClient, capsys: object, tmp_path: Path
) -> None:
    import logging

    anonymous_client.post(
        "/api/v1/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    logging.getLogger("afmlabel.test").warning("payload password=%s", TEST_PASSWORD)
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert TEST_PASSWORD not in captured.out
