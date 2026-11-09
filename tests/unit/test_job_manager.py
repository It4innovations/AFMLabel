# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pytest

from afmlabel.annotations.service import RegionService
from afmlabel.domain.jobs import JobStatus, PointPromptModel, SegmentRequest
from afmlabel.errors import AppError
from afmlabel.imports.batches import BatchStore
from afmlabel.jobs.manager import JobManager, is_out_of_memory
from afmlabel.persistence.rle import decode_rows
from afmlabel.projects.datasets import DatasetStore
from afmlabel.projects.store import ProjectStore
from afmlabel.segmentation.base import (
    BackendCapability,
    BackendDescriptor,
    BackendOutOfMemoryError,
    PointPrompt,
    SegmentationProposal,
)
from afmlabel.segmentation.fake import FakeBackend
from afmlabel.segmentation.registry import BackendRegistry
from tests.fixtures.images import write_pair


class ExplodingBackend:
    backend_id = "exploding"

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.released = 0
        self.fatal: str | None = None

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            backend_id=self.backend_id,
            display_name="Exploding",
            capabilities=frozenset({BackendCapability.POINT_PROMPTS}),
            available=self.fatal is None,
            unavailable_reason=self.fatal,
            model_repository=None,
            model_revision=None,
            checkpoint_sha256=None,
            code_revision=None,
            device="cpu",
            dtype=None,
        )

    def segment(
        self,
        image: np.ndarray,
        *,
        points: list[PointPrompt],
        box_xyxy: object,
        previous_mask: object,
        text_prompt: object,
    ) -> list[SegmentationProposal]:
        raise self.error

    def release(self) -> None:
        self.released += 1

    def mark_fatal(self, reason: str) -> None:
        self.fatal = reason


class SlowBackend(FakeBackend):
    backend_id = "slow"

    def segment(self, image: np.ndarray, **kwargs: object) -> list[SegmentationProposal]:  # type: ignore[override]
        time.sleep(0.4)
        return super().segment(image, **kwargs)  # type: ignore[arg-type]


def _wait(manager: JobManager, job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manager.get(job_id).status.terminal:
            return
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


@pytest.fixture
def env(tmp_path: Path) -> tuple[JobManager, str, str, BackendRegistry]:
    projects = ProjectStore(tmp_path / "ws")
    batches = BatchStore(tmp_path / "ws", max_file_size_bytes=10**8, chunk_size=65536)
    datasets = DatasetStore(projects, batches)
    regions = RegionService(datasets)
    project = projects.create_project("jobs")
    batch = batches.create(project.project_id)
    topo, fric = write_pair(tmp_path / "in", "1")
    with topo.open("rb") as t:
        rt = batches.add_file(batch, "topography", topo.name, t)
    with fric.open("rb") as f:
        rf = batches.add_file(batch, "friction", fric.name, f)
    dataset = datasets.commit_batch(
        project.project_id,
        batch,
        [("1", {"topography": rt.file_id, "friction": rf.file_id})],
        pairing_method="key",
        expected_revision=1,
        actor=None,
    )[0]
    registry = BackendRegistry(
        {
            "fake": FakeBackend(),
            "slow": SlowBackend(),
            "oom": ExplodingBackend(BackendOutOfMemoryError("oom", "CUDA out of memory")),
            "boom": ExplodingBackend(RuntimeError("kaboom")),
        }
    )
    manager = JobManager(tmp_path / "ws", registry, datasets, regions, max_workers=1, max_queue=2)
    return manager, project.project_id, dataset.dataset_id, registry


def test_submit_runs_and_returns_proposals_with_provenance(env: tuple) -> None:
    manager, pid, dsid, _ = env
    request = SegmentRequest(
        backend_id="fake", channel="friction", points=[PointPromptModel(x=32, y=32, positive=True)]
    )
    record = manager.submit(pid, dsid, request, actor="tester")
    assert record.status == JobStatus.QUEUED
    _wait(manager, record.job_id)
    done = manager.get(record.job_id)
    assert done.status == JobStatus.SUCCEEDED and done.result is not None
    assert len(done.result.proposals) == 2
    mask = decode_rows(done.result.proposals[0].mask["counts"], 64, 64)
    assert mask[32, 32] and done.result.proposals[0].area == int(mask.sum())
    prov = done.result.provenance
    assert prov["backend_id"] == "fake" and prov["channel"] == "friction"
    assert prov["preprocessing"]["rgb_replication"] is True and len(prov["source_sha256"]) == 64
    assert prov["prompts"]["points"][0]["x"] == 32
    assert manager.list_for_dataset(pid, dsid)[0].result.proposals[0].mask == {}


def test_unavailable_or_unknown_backend_is_rejected(env: tuple) -> None:
    manager, pid, dsid, _ = env
    with pytest.raises(AppError) as excinfo:
        manager.submit(pid, dsid, SegmentRequest(backend_id="nope"), actor=None)
    assert excinfo.value.code == "MODEL_UNAVAILABLE"


def test_queue_limit_and_cancellation(env: tuple) -> None:
    manager, pid, dsid, _ = env
    req = SegmentRequest(backend_id="slow", points=[PointPromptModel(x=10, y=10)])
    first = manager.submit(pid, dsid, req, actor=None)
    time.sleep(0.1)  # first job starts running
    second = manager.submit(pid, dsid, req, actor=None)
    third = manager.submit(pid, dsid, req, actor=None)
    with pytest.raises(AppError) as excinfo:
        manager.submit(pid, dsid, req, actor=None)
    assert excinfo.value.code == "JOB_QUEUE_FULL"
    cancelled = manager.cancel(third.job_id)
    assert cancelled.status == JobStatus.CANCELLED
    running = manager.cancel(first.job_id)
    assert running.cancel_requested and running.status == JobStatus.RUNNING
    _wait(manager, first.job_id)
    assert manager.get(first.job_id).status == JobStatus.CANCELLED
    assert manager.get(first.job_id).result is None
    _wait(manager, second.job_id)
    assert manager.get(second.job_id).status == JobStatus.SUCCEEDED


def test_failures_are_typed_and_oom_disables_backend(env: tuple) -> None:
    manager, pid, dsid, registry = env
    boom = manager.submit(
        pid,
        dsid,
        SegmentRequest(backend_id="boom", points=[PointPromptModel(x=1, y=1)]),
        actor=None,
    )
    _wait(manager, boom.job_id)
    failed = manager.get(boom.job_id)
    assert failed.status == JobStatus.FAILED and failed.error is not None
    assert failed.error.code == "INTERNAL_ERROR" and "kaboom" in failed.error.message

    backend = registry.get("oom")
    first = manager.submit(
        pid, dsid, SegmentRequest(backend_id="oom", points=[PointPromptModel(x=1, y=1)]), actor=None
    )
    _wait(manager, first.job_id)
    assert manager.get(first.job_id).error.code == "MODEL_OOM"  # type: ignore[union-attr]
    assert backend.released == 1 and backend.descriptor().available  # type: ignore[attr-defined]
    second = manager.submit(
        pid, dsid, SegmentRequest(backend_id="oom", points=[PointPromptModel(x=1, y=1)]), actor=None
    )
    _wait(manager, second.job_id)
    assert not backend.descriptor().available
    assert "out-of-memory" in (backend.descriptor().unavailable_reason or "")
    with pytest.raises(AppError):
        manager.submit(
            pid,
            dsid,
            SegmentRequest(backend_id="oom", points=[PointPromptModel(x=1, y=1)]),
            actor=None,
        )


def test_jobs_are_persisted_and_interrupted_ones_recovered(env: tuple, tmp_path: Path) -> None:
    manager, pid, dsid, registry = env
    record = manager.submit(
        pid,
        dsid,
        SegmentRequest(backend_id="fake", points=[PointPromptModel(x=5, y=5)]),
        actor=None,
    )
    _wait(manager, record.job_id)
    job_files = list((tmp_path / "ws").glob("projects/*/datasets/*/jobs/*.json"))
    assert len(job_files) == 1
    text = job_files[0].read_text(encoding="utf-8")
    text = text.replace('"status":"succeeded"', '"status":"running"').replace(
        '"status": "succeeded"', '"status": "running"'
    )
    job_files[0].write_text(text, encoding="utf-8")
    fresh = JobManager(tmp_path / "ws", registry, manager._datasets, manager._regions)  # noqa: SLF001
    assert fresh.recover_interrupted_jobs() == 1
    assert '"failed"' in job_files[0].read_text(encoding="utf-8")


def test_oom_detection_heuristics() -> None:
    assert is_out_of_memory(MemoryError())
    assert is_out_of_memory(RuntimeError("CUDA error: out of memory"))
    assert not is_out_of_memory(RuntimeError("shape mismatch"))
