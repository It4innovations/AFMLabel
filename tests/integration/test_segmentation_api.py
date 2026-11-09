# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from tests.integration.test_regions_api import setup_dataset


def _wait(client: TestClient, base: str, job_id: str) -> dict:
    for _ in range(200):
        record = client.get(f"{base}/jobs/{job_id}").json()
        if record["status"] in ("succeeded", "failed", "cancelled"):
            return record
        time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_segment_job_and_accept_proposal(api_client: TestClient, tmp_path: Path) -> None:
    pid, dsid, classes = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    submitted = api_client.post(
        f"{base}/segment",
        json={
            "backend_id": "fake",
            "channel": "topography",
            "points": [{"x": 20, "y": 20, "positive": True}],
        },
    )
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert job["status"] in ("queued", "running") and job["backend_id"] == "fake"
    record = _wait(api_client, base, job["job_id"])
    assert record["status"] == "succeeded"
    proposal = record["result"]["proposals"][0]
    assert proposal["area"] > 0 and proposal["mask"]["format"] == "rle_rows"
    provenance = record["result"]["provenance"]
    assert provenance["backend_id"] == "fake" and provenance["job_id"] == job["job_id"]

    # Accepting is an explicit region creation carrying the provenance.
    created = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 1,
            "class_id": classes["Layer A"],
            "mask": proposal["mask"],
            "source": "sam_proposal",
            "provenance": provenance,
        },
    )
    assert created.status_code == 201, created.text
    region = created.json()["region"]
    assert region["source"] == "sam_proposal" and region["provenance"]["backend_id"] == "fake"
    events = api_client.get(f"/api/v1/projects/{pid}/events").json()
    assert any(e["operation"] == "region.created" and e["source"] == "sam_proposal" for e in events)

    # Without provenance a proposal region is refused.
    refused = api_client.post(
        f"{base}/regions",
        json={
            "expected_revision": 2,
            "class_id": classes["Layer A"],
            "mask": proposal["mask"],
            "source": "sam_proposal",
        },
    )
    assert refused.status_code == 422

    listing = api_client.get(f"{base}/jobs").json()
    assert (
        listing[0]["job_id"] == job["job_id"] and listing[0]["result"]["proposals"][0]["mask"] == {}
    )


def test_segment_rejects_unavailable_backend_and_empty_prompts(
    api_client: TestClient, tmp_path: Path
) -> None:
    pid, dsid, _ = setup_dataset(api_client, tmp_path)
    base = f"/api/v1/projects/{pid}/datasets/{dsid}"
    unavailable = api_client.post(
        f"{base}/segment", json={"backend_id": "sam31", "points": [{"x": 1, "y": 1}]}
    )
    assert unavailable.status_code == 409
    assert unavailable.json()["error"]["code"] == "MODEL_UNAVAILABLE"
    assert "disabled in configuration" in unavailable.json()["error"]["message"]
    empty = api_client.post(f"{base}/segment", json={"backend_id": "fake"})
    assert empty.status_code == 422
    unsupported = api_client.post(
        f"{base}/segment", json={"backend_id": "fake", "text_prompt": "layer"}
    )
    record = _wait(api_client, base, unsupported.json()["job_id"])
    assert (
        record["status"] == "failed" and record["error"]["code"] == "MODEL_CAPABILITY_UNSUPPORTED"
    )
    missing = api_client.get(f"{base}/jobs/job_doesnotexist")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "JOB_NOT_FOUND"
