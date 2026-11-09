# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Acceptance: no external request occurs during a startup smoke
test. Every socket connection attempted while the app is created and the
endpoints are exercised must target the loopback interface."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from afmlabel.auth.account import AccountStore
from afmlabel.main import create_app
from afmlabel.settings import AppSettings
from tests.conftest import CSRF_HEADERS, TEST_PASSWORD, TEST_USERNAME, login


def test_startup_and_health_make_no_external_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempted: list[object] = []
    original_connect = socket.socket.connect

    def recording_connect(self: socket.socket, address: object) -> None:
        attempted.append(address)
        host = address[0] if isinstance(address, tuple) else str(address)
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"external connection attempted: {address!r}")
        original_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", recording_connect)
    settings = AppSettings(workspace={"root": tmp_path / "ws"})
    AccountStore(settings.workspace.root).set_password(TEST_USERNAME, TEST_PASSWORD)
    app = create_app(settings)
    client = TestClient(app)
    client.headers.update(CSRF_HEADERS)
    assert client.get("/api/v1/health").status_code == 200
    login(client)
    assert client.get("/api/v1/models").status_code == 200
    assert client.get("/api/v1/system/resources").status_code == 200
    assert client.get("/").status_code == 200
    assert client.post("/api/v1/projects", json={"name": "p"}).status_code == 201
    # Anything that did connect (anyio/psutil internals) was loopback-only,
    # otherwise ``recording_connect`` would have raised.
    for address in attempted:
        host = address[0] if isinstance(address, tuple) else str(address)
        assert host in ("127.0.0.1", "::1", "localhost")
