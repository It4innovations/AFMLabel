# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from afmlabel import __version__


def test_health_reports_every_check(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in ("ok", "degraded", "error")
    assert body["package_version"] == __version__
    for key in ("python", "filesystem", "memory", "disk", "gpu", "torch"):
        assert body[key]["status"] in ("ok", "degraded", "missing", "error"), key
    assert body["filesystem"]["status"] == "ok"


def test_version_endpoint(api_client: TestClient) -> None:
    body = api_client.get("/api/v1/version").json()
    assert body["version"] == __version__
    assert body["api_version"] == "v1"


def test_resources_endpoint(api_client: TestClient) -> None:
    body = api_client.get("/api/v1/system/resources").json()
    assert body["memory"]["total_bytes"] > 0
    assert "disk" in body and "gpu" in body and "warnings" in body


def test_models_lists_fake_and_sam31_placeholder(api_client: TestClient) -> None:
    body = api_client.get("/api/v1/models").json()
    by_id = {b["backend_id"]: b for b in body["backends"]}
    assert by_id["fake"]["available"] is True
    assert "point_prompts" in by_id["fake"]["capabilities"]
    assert by_id["sam31"]["available"] is False
    assert "disabled in configuration" in by_id["sam31"]["unavailable_reason"]
    assert by_id["sam31"]["model_repository"] == "facebook/sam3.1"
    assert by_id["sam31"]["load_state"] == "unavailable"
    assert by_id["fake"]["load_state"] == "loaded"
    assert body["default_backend_id"] == "fake"


def test_model_load_endpoint(api_client: TestClient) -> None:
    from tests.conftest import CSRF_HEADERS

    ok = api_client.post("/api/v1/models/fake/load", headers=CSRF_HEADERS)
    assert ok.status_code == 202
    assert ok.json()["load_state"] == "loaded"

    unavailable = api_client.post("/api/v1/models/sam31/load", headers=CSRF_HEADERS)
    assert unavailable.status_code == 409
    error = unavailable.json()["error"]
    assert error["code"] == "MODEL_UNAVAILABLE"
    assert "disabled in configuration" in error["message"]

    unknown = api_client.post("/api/v1/models/nope/load", headers=CSRF_HEADERS)
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "MODEL_UNAVAILABLE"


def test_unknown_route_uses_error_envelope(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_index_page_and_static_assets_are_local(api_client: TestClient) -> None:
    page = api_client.get("/")
    assert page.status_code == 200
    html = page.text
    assert "AFMLabel" in html
    for url in re.findall(r"""(?:src|href)=["']([^"']+)""", html):
        if url.startswith("http"):
            host = urlsplit(url).hostname
            # Assets are served from this host; the only external URL is the
            # navigation link on the IT4I brand logo (nothing is loaded from it).
            assert host in ("testserver", "blender.it4i.cz"), url
    css = api_client.get("/static/css/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    js = api_client.get("/static/js/shell.js")
    assert js.headers["content-type"].startswith("text/javascript")
