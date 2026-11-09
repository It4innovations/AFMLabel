# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Single-user local inference job manager.

* one worker thread by default (``runtime.maximum_concurrent_inference_jobs``);
* bounded queue (``runtime.maximum_queued_inference_jobs``), ``JOB_QUEUE_FULL``
  when exceeded;
* cooperative cancellation: a queued job is cancelled immediately, a running
  job finishes its current inference and is then reported cancelled;
* every state change is persisted atomically under the dataset ``jobs/``
  directory so a restart can mark interrupted jobs as failed;
* out-of-memory errors are typed (``MODEL_OOM``), the backend is released,
  and two consecutive OOMs mark the backend unavailable for the rest of
  the process (never silently switch to another model).

Model inference runs entirely in the worker thread; FastAPI handlers only
enqueue and poll.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from afmlabel.annotations.service import RegionService, region_mask
from afmlabel.domain.common import utc_now_iso
from afmlabel.domain.jobs import (
    JobError,
    JobRecord,
    JobStatus,
    ProposalOut,
    SegmentRequest,
    SegmentResult,
)
from afmlabel.errors import AppError, NotFoundError
from afmlabel.images.display import DEFAULT_DISPLAY, to_rgb_uint8
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_model, save_model
from afmlabel.persistence.rle import bounding_box, encode_rows
from afmlabel.projects.datasets import DatasetStore
from afmlabel.segmentation.base import (
    BackendCapabilityError,
    BackendOutOfMemoryError,
    BackendUnavailableError,
    PointPrompt,
)
from afmlabel.segmentation.registry import BackendRegistry, UnknownBackendError

logger = logging.getLogger(__name__)

_OOM_NAMES = ("OutOfMemoryError", "CudaOutOfMemoryError", "MemoryError")


def is_out_of_memory(exc: BaseException) -> bool:
    if isinstance(exc, MemoryError | BackendOutOfMemoryError):
        return True
    name = type(exc).__name__
    if name in _OOM_NAMES:
        return True
    text = str(exc).lower()
    return "out of memory" in text or "cuda error: out of memory" in text


class JobNotFoundError(NotFoundError):
    def __init__(self, job_id: str) -> None:
        super().__init__("JOB_NOT_FOUND", f"job {job_id} does not exist", target={"job_id": job_id})


class JobManager:
    def __init__(
        self,
        workspace_root: Path,
        registry: BackendRegistry,
        datasets: DatasetStore,
        regions: RegionService,
        *,
        max_workers: int = 1,
        max_queue: int = 8,
        oom_fatal_threshold: int = 2,
        event_recorder: Callable[..., Any] | None = None,
    ) -> None:
        self._root = workspace_root
        self._registry = registry
        self._datasets = datasets
        self._regions = regions
        self._max_queue = max(1, max_queue)
        self._oom_threshold = oom_fatal_threshold
        self._record_event = event_recorder
        self._queue: queue.Queue[str] = queue.Queue()
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._oom_streak: dict[str, int] = {}
        self._workers = [
            threading.Thread(target=self._worker, name=f"afmlabel-infer-{i}", daemon=True)
            for i in range(max(1, max_workers))
        ]
        for worker in self._workers:
            worker.start()

    # --------------------------------------------------------------- storage

    def _job_path(self, record: JobRecord) -> Path:
        ddir = self._datasets.dataset_dir(record.project_id, record.dataset_id)
        return paths.dataset_jobs_dir(ddir) / f"{record.job_id}.json"

    def _persist(self, record: JobRecord) -> None:
        try:
            save_model(self._job_path(record), record, fsync=False)
        except (OSError, AppError) as exc:  # dataset may have been deleted meanwhile
            logger.warning(
                "could not persist job", extra={"job_id": record.job_id, "error": str(exc)}
            )

    def recover_interrupted_jobs(self) -> int:
        """Mark jobs left queued/running by a previous process as failed."""
        recovered = 0
        root = paths.projects_root(self._root)
        if not root.exists():
            return 0
        for job_file in root.glob("*/datasets/*/jobs/*.json"):
            try:
                record = load_model(job_file, JobRecord)
            except (OSError, ValueError):
                continue
            if record.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                record.status = JobStatus.FAILED
                record.finished_at = utc_now_iso()
                record.error = JobError(
                    code="INTERNAL_ERROR",
                    message="interrupted by an application restart",
                    recovery="submit the segmentation request again",
                )
                save_model(job_file, record, fsync=False)
                recovered += 1
        if recovered:
            logger.warning(
                "marked interrupted inference jobs as failed", extra={"count": recovered}
            )
        return recovered

    # ------------------------------------------------------------------ API

    def submit(
        self, project_id: str, dataset_id: str, request: SegmentRequest, *, actor: str | None
    ) -> JobRecord:
        try:
            self._registry.get(request.backend_id)
        except UnknownBackendError as exc:
            raise AppError(
                "MODEL_UNAVAILABLE",
                str(exc),
                status_code=404,
                target={"backend_id": request.backend_id},
                recovery="pick a backend listed by /api/v1/models",
            ) from exc
        descriptor = self._registry.describe(request.backend_id)
        if not descriptor.available:
            recovery = (
                "use another backend or set the model up (README.md, Optional: local SAM backends)"
            )
            if descriptor.load_state in ("not_loaded", "loading"):
                recovery = (
                    "load the model first (POST /api/v1/models/{id}/load) and wait until "
                    "it reports load_state loaded"
                )
            raise AppError(
                "MODEL_UNAVAILABLE",
                f"backend {request.backend_id} is unavailable "
                f"({descriptor.load_state}): {descriptor.unavailable_reason}",
                status_code=409,
                target={"backend_id": request.backend_id},
                recovery=recovery,
            )
        self._datasets.get(project_id, dataset_id)
        with self._lock:
            queued = sum(1 for j in self._jobs.values() if j.status == JobStatus.QUEUED)
            if queued >= self._max_queue:
                raise AppError(
                    "JOB_QUEUE_FULL",
                    f"{queued} inference jobs are already queued",
                    status_code=429,
                    recovery="wait for running jobs or cancel queued ones",
                )
            record = JobRecord(
                project_id=project_id,
                dataset_id=dataset_id,
                backend_id=request.backend_id,
                request=request,
                actor=actor,
            )
            self._jobs[record.job_id] = record
        self._persist(record)
        self._queue.put(record.job_id)
        return record

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
        if record is None:
            raise JobNotFoundError(job_id)
        return record

    def list_for_dataset(
        self, project_id: str, dataset_id: str, limit: int = 20
    ) -> list[JobRecord]:
        with self._lock:
            records = [
                j
                for j in self._jobs.values()
                if j.project_id == project_id and j.dataset_id == dataset_id
            ]
        records.sort(key=lambda j: j.created_at, reverse=True)
        return [r.summary() for r in records[:limit]]

    def cancel(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise JobNotFoundError(job_id)
            if record.status == JobStatus.QUEUED:
                record.status = JobStatus.CANCELLED
                record.finished_at = utc_now_iso()
                record.message = "cancelled before it started"
            elif record.status == JobStatus.RUNNING:
                record.cancel_requested = True
                record.message = "cancellation requested; the running inference finishes first"
        self._persist(record)
        return record

    def shutdown(self) -> None:
        self._stop.set()

    # --------------------------------------------------------------- worker

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            with self._lock:
                record = self._jobs.get(job_id)
                if record is None or record.status != JobStatus.QUEUED:
                    continue
                record.status = JobStatus.RUNNING
                record.started_at = utc_now_iso()
                record.progress = 0.05
                record.message = "loading source image"
            self._persist(record)
            try:
                result = self._run(record)
            except BaseException as exc:  # noqa: BLE001 - every failure becomes a typed job error
                self._fail(record, exc)
            else:
                with self._lock:
                    record.result = result
                    record.progress = 1.0
                    if record.cancel_requested:
                        record.status = JobStatus.CANCELLED
                        record.message = "cancelled; result discarded"
                        record.result = None
                    else:
                        record.status = JobStatus.SUCCEEDED
                        record.message = f"{len(result.proposals)} proposal(s)"
                    record.finished_at = utc_now_iso()
                self._oom_streak[record.backend_id] = 0
                self._persist(record)

    def _fail(self, record: JobRecord, exc: BaseException) -> None:
        backend_id = record.backend_id
        if is_out_of_memory(exc):
            streak = self._oom_streak.get(backend_id, 0) + 1
            self._oom_streak[backend_id] = streak
            error = JobError(
                code="MODEL_OOM",
                message=f"inference ran out of memory: {exc}",
                recovery="close other GPU/RAM consumers or use a smaller prompt region",
            )
            try:
                self._registry.get(backend_id).release()
            except Exception:  # noqa: BLE001
                pass
            if streak >= self._oom_threshold:
                backend = self._registry.get(backend_id)
                mark = getattr(backend, "mark_fatal", None)
                if callable(mark):
                    mark(f"{streak} consecutive out-of-memory failures; restart AFMLabel to retry")
                    error.recovery = "backend disabled after repeated OOM; restart the application"
        elif isinstance(exc, BackendCapabilityError):
            error = JobError(
                code=exc.code, message=str(exc), recovery="use a supported prompt kind"
            )
        elif isinstance(exc, BackendUnavailableError):
            error = JobError(code=exc.code, message=str(exc))
        elif isinstance(exc, AppError):
            error = JobError(code=exc.code, message=exc.message, recovery=exc.recovery)
        else:
            logger.exception("inference job failed", extra={"job_id": record.job_id})
            error = JobError(code="INTERNAL_ERROR", message=f"{type(exc).__name__}: {exc}")
        with self._lock:
            record.status = JobStatus.FAILED
            record.error = error
            record.finished_at = utc_now_iso()
            record.message = error.message
        self._persist(record)
        if self._record_event is not None:
            try:
                self._record_event(
                    record.project_id,
                    operation="job.failed",
                    dataset_id=record.dataset_id,
                    source="system",
                    actor=record.actor,
                    details={"job_id": record.job_id, "code": error.code, "backend_id": backend_id},
                )
            except Exception:  # noqa: BLE001
                pass

    def _run(self, record: JobRecord) -> SegmentResult:
        request = record.request
        backend = self._registry.get(request.backend_id)
        dataset = self._datasets.get(record.project_id, record.dataset_id)
        channel = request.channel or dataset.channel_keys[0]
        decoded = self._datasets.load_source_array(dataset, channel)
        image = to_rgb_uint8(decoded.array, DEFAULT_DISPLAY)
        del decoded
        previous: np.ndarray | None = None
        if request.previous_region_id:
            _, region_set = self._regions.load(record.project_id, record.dataset_id)
            region = region_set.by_id(request.previous_region_id)
            if region is None:
                raise AppError(
                    "REGION_REFERENCE_INVALID",
                    f"region {request.previous_region_id} does not exist",
                    status_code=404,
                )
            previous = region_mask(region)
        with self._lock:
            record.progress = 0.3
            record.message = f"running {request.backend_id}"
        self._persist(record)
        points = [PointPrompt(p.x, p.y, p.positive) for p in request.points]
        box = tuple(request.box_xyxy) if request.box_xyxy else None
        proposals = backend.segment(
            image,
            points=points,
            box_xyxy=box,  # type: ignore[arg-type]
            previous_mask=previous,
            text_prompt=request.text_prompt,
        )
        descriptor = backend.descriptor()
        provenance = {
            "job_id": record.job_id,
            "backend_id": descriptor.backend_id,
            "model_repository": descriptor.model_repository,
            "model_revision": descriptor.model_revision,
            "checkpoint_sha256": descriptor.checkpoint_sha256,
            "code_revision": descriptor.code_revision,
            "device": descriptor.device,
            "dtype": descriptor.dtype,
            "channel": channel,
            "modality": channel,  # legacy spelling kept for older provenance readers
            "source_sha256": dataset.source(channel).sha256,
            "preprocessing": {"rgb_replication": True, **DEFAULT_DISPLAY.as_dict()},
            "prompts": request.model_dump(exclude={"backend_id"}),
            "finished_at": utc_now_iso(),
        }
        outputs: list[ProposalOut] = []
        for proposal in proposals:
            mask = np.asarray(proposal.mask, dtype=bool)
            if mask.shape != (dataset.height, dataset.width):
                raise AppError(
                    "VALIDATION_ERROR",
                    f"backend returned a {mask.shape} mask for a "
                    f"{dataset.height}x{dataset.width} image",
                    status_code=500,
                )
            outputs.append(
                ProposalOut(
                    mask={
                        "format": "rle_rows",
                        "size": [dataset.height, dataset.width],
                        "counts": encode_rows(mask),
                    },
                    score=proposal.score,
                    area=int(mask.sum()),
                    bbox_xywh=bounding_box(mask),
                    backend_metadata={
                        k: (v if isinstance(v, str | int | float | bool | None) else str(v))
                        for k, v in proposal.backend_metadata.items()
                    },
                )
            )
        return SegmentResult(proposals=outputs, provenance=provenance)
