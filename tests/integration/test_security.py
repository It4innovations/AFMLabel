# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Hardening: path traversal, project-scoped downloads, secret
redaction in responses, resource-metric failure tolerance."""

from __future__ import annotations

import logging
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from afmlabel.main import create_app
from afmlabel.settings import AppSettings
from tests.conftest import CSRF_HEADERS, create_project, login
from tests.integration.test_datasets_api import import_pairs


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/projects/..%2F..%2Fauth",
        "/api/v1/projects/proj_x%2F..%2F..%2Fauth%2Faccount.json",
        "/api/v1/projects/../../auth/account.json",
        "/api/v1/projects/proj_0000000000/datasets/..%2F..%2Fproject.json",
        "/api/v1/projects/proj_0000000000/datasets/ds_x%2F..%2F..%2F..%2Fauth/image/topography",
        "/static/../config/app.example.yaml",
        "/static/..%2F..%2Fpyproject.toml",
    ],
)
def test_path_traversal_attempts_are_rejected(api_client: TestClient, path: str) -> None:
    response = api_client.get(path)
    assert response.status_code in (400, 404, 422), response.text
    assert "password_hash" not in response.text
    assert "hatchling" not in response.text


def test_downloads_require_login_and_project_scope(
    app_settings: AppSettings, configured_account: object, tmp_path: Path
) -> None:
    client = TestClient(create_app(app_settings))
    client.headers.update(CSRF_HEADERS)
    login(client)
    pid = create_project(client)["project_id"]
    dsid = import_pairs(client, pid, tmp_path / "in", ["1"], revision=1)["datasets"][0][
        "dataset_id"
    ]
    other = create_project(client, "other")["project_id"]

    assert (
        client.get(f"/api/v1/projects/{pid}/datasets/{dsid}/source/topography").status_code == 200
    )
    cross = client.get(f"/api/v1/projects/{other}/datasets/{dsid}/source/topography")
    assert cross.status_code == 404 and cross.json()["error"]["code"] == "DATASET_NOT_FOUND"

    anonymous = TestClient(create_app(app_settings))
    for url in (
        f"/api/v1/projects/{pid}/datasets/{dsid}/source/topography",
        f"/api/v1/projects/{pid}/datasets/{dsid}/image/friction",
        f"/api/v1/projects/{pid}/datasets/{dsid}/thumbnail/topography",
        f"/api/v1/projects/{pid}/events",
        "/api/v1/system/resources",
        "/api/v1/models",
    ):
        response = anonymous.get(url)
        assert response.status_code == 401, url
        assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    page = anonymous.get(f"/projects/{pid}/datasets/{dsid}", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"].startswith("/login")


def test_secrets_never_appear_in_error_responses_or_logs(
    api_client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_SECRETSECRETSECRETSECRET1234")
    from afmlabel.privacy.redaction import registry

    registry.register_from_environment()
    with caplog.at_level(logging.INFO):
        logging.getLogger("afmlabel.test").info(
            "Authorization: Bearer hf_SECRETSECRETSECRETSECRET1234 cookie: afmlabel_session=abc"
        )
    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "hf_SECRET" not in joined and "afmlabel_session=abc" not in joined
    # Error envelopes are redacted even when a secret slips into a message.
    from afmlabel.api.errors import error_response

    envelope = error_response(
        "AUTH_INVALID", "token hf_SECRETSECRETSECRETSECRET1234 rejected", 401
    ).body.decode()
    assert "hf_SECRETSECRET" not in envelope


def test_resource_metric_failure_does_not_break_editor(
    app_settings: AppSettings, configured_account: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("no memory metrics on this host")

    monkeypatch.setattr(psutil, "virtual_memory", boom)
    client = TestClient(create_app(app_settings))
    client.headers.update(CSRF_HEADERS)
    login(client)
    resources = client.get("/api/v1/system/resources").json()
    assert resources["memory"]["error"]["code"] == "RESOURCE_METRIC_UNAVAILABLE"
    assert any(w["code"] == "RESOURCE_METRIC_UNAVAILABLE" for w in resources["warnings"])
    health = client.get("/api/v1/health").json()
    assert health["memory"]["status"] == "degraded"
    # Annotation workflow keeps working.
    assert client.post("/api/v1/projects", json={"name": "still works"}).status_code == 201


def test_low_ram_and_disk_warnings_are_reported(
    app_settings: AppSettings, configured_account: object
) -> None:
    app_settings.resources.low_ram_warning_gb = 10**6
    app_settings.resources.low_disk_warning_gb = 10**6
    client = TestClient(create_app(app_settings))
    client.headers.update(CSRF_HEADERS)
    login(client)
    warnings = {w["code"] for w in client.get("/api/v1/system/resources").json()["warnings"]}
    assert {"RAM_LOW", "DISK_SPACE_LOW"} <= warnings
    assert client.get("/api/v1/health").json()["memory"]["status"] == "degraded"
