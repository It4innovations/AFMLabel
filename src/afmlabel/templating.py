# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Shared Jinja2 environment for server-rendered pages and cache-safe static
asset URLs.

Static files are served with ``Cache-Control: no-cache`` (the browser must
revalidate; Starlette answers ``304`` from ``ETag``/``Last-Modified`` when
nothing changed) and every stylesheet/script URL rendered by a template
carries a content-hash query string (``/static/css/app.css?v=1a2b3c4d5e``),
so an upgraded application never runs with a stale cached asset.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

_version_cache: dict[str, tuple[float, int, str]] = {}
_version_lock = threading.Lock()


def asset_version(relative_path: str) -> str:
    """Short content hash of a static asset (cached by mtime/size)."""
    path = STATIC_DIR / relative_path
    try:
        stat = path.stat()
    except OSError:
        return "missing"
    key = str(path)
    with _version_lock:
        cached = _version_cache.get(key)
        if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return cached[2]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
    with _version_lock:
        _version_cache[key] = (stat.st_mtime, stat.st_size, digest)
    return digest


def asset_url(relative_path: str) -> str:
    """``/static/<path>?v=<content hash>`` for use in templates."""
    return f"/static/{relative_path}?v={asset_version(relative_path)}"


def import_map_json() -> str:
    """Import map that maps every ES module under ``static/js`` to its
    content-hashed URL, so ``import ... from "./x.js"`` inside modules is
    cache-busted exactly like the entry script."""
    imports = {
        f"/static/js/{path.name}": asset_url(f"js/{path.name}")
        for path in sorted((STATIC_DIR / "js").glob("*.js"))
    }
    return json.dumps({"imports": imports}, separators=(",", ":"))


templates.env.globals["asset_url"] = asset_url
templates.env.globals["import_map_json"] = import_map_json


class RevalidatingStaticFiles(StaticFiles):
    """StaticFiles that forbids serving stale copies without revalidation."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = "no-cache"
        return response

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response


def static_files() -> Any:
    return RevalidatingStaticFiles(directory=str(STATIC_DIR))
