# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Static assets must never be served stale: content-hash URLs in pages and
``Cache-Control: no-cache`` on the files themselves."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from afmlabel.templating import asset_url, asset_version


def test_asset_url_carries_content_hash() -> None:
    version = asset_version("css/app.css")
    assert re.fullmatch(r"[0-9a-f]{10}", version)
    assert asset_url("css/app.css") == f"/static/css/app.css?v={version}"
    assert asset_version("css/does-not-exist.css") == "missing"


def test_login_page_links_versioned_assets(api_client: TestClient) -> None:
    response = api_client.get("/login")
    assert response.status_code == 200
    assert re.search(r'/static/css/app\.css\?v=[0-9a-f]{10}"', response.text)
    assert re.search(r'/static/js/shell\.js\?v=[0-9a-f]{10}"', response.text)
    # Module imports inside scripts are cache-busted through an import map.
    match = re.search(r'<script type="importmap">(\{.*?\})</script>', response.text)
    assert match, "import map missing"
    import json

    imports = json.loads(match.group(1))["imports"]
    assert re.fullmatch(r"/static/js/api\.js\?v=[0-9a-f]{10}", imports["/static/js/api.js"])
    assert "/static/js/segmentation.js" in imports


def test_static_files_require_revalidation(api_client: TestClient) -> None:
    response = api_client.get("/static/css/app.css")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert "etag" in response.headers
    assert response.headers["content-type"].startswith("text/css")
    cached = api_client.get(
        "/static/css/app.css", headers={"If-None-Match": response.headers["etag"]}
    )
    assert cached.status_code == 304
    assert cached.headers.get("cache-control") == "no-cache"
