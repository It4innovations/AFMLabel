# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from fastapi.testclient import TestClient

from afmlabel.main import create_app
from afmlabel.settings import AppSettings
from tests.conftest import create_project, login


def test_project_crud_and_events(api_client: TestClient) -> None:
    project = create_project(api_client, "AFM batch 1")
    pid = project["project_id"]
    assert project["revision"] == 1

    listing = api_client.get("/api/v1/projects").json()
    assert listing[0]["project_id"] == pid

    detail = api_client.get(f"/api/v1/projects/{pid}").json()
    assert detail["project"]["name"] == "AFM batch 1"
    assert detail["classes"]["classes"] == []
    assert detail["effective_boundary_mode"] == "disabled"
    assert detail["lock"]["locked"] is False

    renamed = api_client.patch(
        f"/api/v1/projects/{pid}", json={"expected_revision": 1, "name": "AFM batch 2"}
    )
    assert renamed.status_code == 200 and renamed.json()["revision"] == 2

    stale = api_client.patch(f"/api/v1/projects/{pid}", json={"expected_revision": 1, "name": "x"})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert [e["operation"] for e in events] == ["project.create", "project.update"]
    assert events[-1]["actor"].startswith("tester@")

    assert api_client.get(f"/projects/{pid}").status_code == 200

    deleted = api_client.delete(f"/api/v1/projects/{pid}?expected_revision=2")
    assert deleted.status_code == 204
    assert api_client.get(f"/api/v1/projects/{pid}").status_code == 404


def test_project_persists_across_app_restart(
    app_settings: AppSettings, configured_account: object
) -> None:
    client = TestClient(create_app(app_settings))
    client.headers.update({"X-Requested-With": "AFMLabel"})
    login(client)
    pid = create_project(client, "durable")["project_id"]
    client.put(
        f"/api/v1/projects/{pid}/ui-state",
        json={"channel": "friction", "zoom": 3.0, "overlay_opacity": 0.4, "brush_radius": 5},
    )

    restarted = TestClient(create_app(app_settings))
    restarted.headers.update({"X-Requested-With": "AFMLabel"})
    login(restarted)
    detail = restarted.get(f"/api/v1/projects/{pid}").json()
    assert detail["project"]["name"] == "durable"
    assert detail["ui_state"]["channel"] == "friction"
    assert detail["ui_state"]["zoom"] == 3.0


def test_class_ontology_endpoints(api_client: TestClient) -> None:
    pid = create_project(api_client)["project_id"]
    created = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={"expected_revision": 1, "name": "Layer A", "display_color": "#FF0000"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["project"]["revision"] == 2
    cls = body["classes"]["classes"][0]
    assert cls["class_id"] == 1 and cls["stable_key"] == "layer_a"
    assert cls["display_color"] == "#ff0000"

    duplicate = api_client.post(
        f"/api/v1/projects/{pid}/classes", json={"expected_revision": 2, "name": "layer a"}
    )
    assert duplicate.status_code == 409

    boundary = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={"expected_revision": 2, "name": "Boundary", "semantic_role": "Boundary"},
    ).json()
    second_boundary = api_client.post(
        f"/api/v1/projects/{pid}/classes",
        json={"expected_revision": 3, "name": "Edge", "semantic_role": "Boundary"},
    )
    assert second_boundary.status_code == 409
    assert second_boundary.json()["error"]["code"] == "BOUNDARY_POLICY_VIOLATION"

    patched = api_client.patch(
        f"/api/v1/projects/{pid}/classes/1",
        json={
            "expected_revision": boundary["project"]["revision"],
            "name": "Layer A2",
            "export_id": 10,
        },
    ).json()
    assert patched["classes"]["classes"][0]["name"] == "Layer A2"
    assert patched["classes"]["classes"][0]["export_id"] == 10

    missing = api_client.patch(
        f"/api/v1/projects/{pid}/classes/99",
        json={"expected_revision": patched["project"]["revision"], "name": "x"},
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CLASS_REFERENCE_INVALID"

    deleted = api_client.delete(
        f"/api/v1/projects/{pid}/classes/1?expected_revision={patched['project']['revision']}"
    ).json()
    assert [c["class_id"] for c in deleted["classes"]["classes"]] == [2]
    # Class ids are never reused.
    assert deleted["classes"]["next_class_id"] == 3


def test_project_lock_lifecycle(api_client: TestClient) -> None:
    pid = create_project(api_client)["project_id"]
    tab_a = {"X-AFMLabel-Editor": "tab-a"}
    tab_b = {"X-AFMLabel-Editor": "tab-b"}

    acquired = api_client.post(f"/api/v1/projects/{pid}/lock", headers=tab_a)
    assert acquired.status_code == 200 and acquired.json()["owner_id"] == "tab-a"

    blocked = api_client.post(f"/api/v1/projects/{pid}/lock", headers=tab_b)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "PROJECT_LOCKED"

    edit_from_b = api_client.patch(
        f"/api/v1/projects/{pid}", json={"expected_revision": 1, "name": "b"}, headers=tab_b
    )
    assert edit_from_b.status_code == 409

    status_b = api_client.get(f"/api/v1/projects/{pid}/lock", headers=tab_b).json()
    assert status_b["locked"] and not status_b["owned_by_caller"] and not status_b["stale"]

    assert (
        api_client.post(f"/api/v1/projects/{pid}/lock/heartbeat", headers=tab_a).status_code == 200
    )

    takeover = api_client.post(f"/api/v1/projects/{pid}/lock", json={"force": True}, headers=tab_b)
    assert takeover.status_code == 200 and takeover.json()["owner_id"] == "tab-b"
    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert events[-1]["operation"] == "lock.takeover"

    assert api_client.delete(f"/api/v1/projects/{pid}/lock", headers=tab_b).status_code == 204
    assert api_client.get(f"/api/v1/projects/{pid}/lock").json()["locked"] is False


def test_unknown_project_returns_typed_404(api_client: TestClient) -> None:
    response = api_client.get("/api/v1/projects/proj_doesnotexist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"
