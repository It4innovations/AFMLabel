# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Backend discovery, availability status and explicit model loading.

``fake`` is always present. The model backends (``sam3``, ``sam31``,
``sam21``) are always *listed* so the UI and ``/api/v1/models`` can show an
exact unavailable reason instead of silently omitting a model or
substituting another one. There is no fallback between backends.

Real models are never loaded at import or application start-up: a load
takes tens of seconds and gigabytes of memory, and two large models do not
fit one workstation GPU. Loading is an explicit, per-backend action
(:meth:`BackendRegistry.start_load`, exposed as
``POST /api/v1/models/{backend_id}/load``) that runs in a background thread
so the event loop stays responsive; ``list_descriptors`` reports the
``load_state`` of every backend so the workspace can offer the load and
wait for it.
"""

from __future__ import annotations

import dataclasses
import logging
import threading

from afmlabel.segmentation.base import BackendDescriptor, SegmentationBackend
from afmlabel.segmentation.fake import FakeBackend
from afmlabel.segmentation.sam3 import Sam3Backend
from afmlabel.segmentation.sam21 import Sam21Backend
from afmlabel.segmentation.sam31 import Sam31Backend
from afmlabel.settings import AppSettings

logger = logging.getLogger(__name__)


class UnknownBackendError(KeyError):
    code = "MODEL_UNAVAILABLE"

    def __init__(self, backend_id: str, known_backend_ids: list[str]) -> None:
        self.backend_id = backend_id
        self.known_backend_ids = known_backend_ids
        super().__init__(f"unknown backend_id {backend_id!r}; known backends: {known_backend_ids}")


class BackendNotLoadableError(RuntimeError):
    """The backend cannot be loaded: it reports a configuration/stack reason."""

    def __init__(self, backend_id: str, reason: str) -> None:
        self.backend_id = backend_id
        self.reason = reason
        super().__init__(f"backend {backend_id} cannot be loaded: {reason}")


class BackendRegistry:
    def __init__(self, backends: dict[str, SegmentationBackend]) -> None:
        self._backends = dict(backends)
        self._lock = threading.Lock()
        self._loaders: dict[str, threading.Thread] = {}
        self._attempted: set[str] = set()

    def get(self, backend_id: str) -> SegmentationBackend:
        try:
            return self._backends[backend_id]
        except KeyError:
            raise UnknownBackendError(backend_id, sorted(self._backends)) from None

    # ------------------------------------------------------------ loading

    def load_state(self, backend_id: str) -> str:
        backend = self.get(backend_id)
        descriptor = backend.descriptor()
        if descriptor.available:
            return "loaded"
        with self._lock:
            thread = self._loaders.get(backend_id)
            loading = thread is not None and thread.is_alive()
            attempted = backend_id in self._attempted
        if loading:
            return "loading"
        reason_fn = getattr(backend, "unavailable_reason", None)
        reason = reason_fn() if callable(reason_fn) else descriptor.unavailable_reason
        if reason is None:
            return "not_loaded"
        return "failed" if attempted else "unavailable"

    def describe(self, backend_id: str) -> BackendDescriptor:
        descriptor = self.get(backend_id).descriptor()
        return dataclasses.replace(descriptor, load_state=self.load_state(backend_id))

    def start_load(self, backend_id: str) -> BackendDescriptor:
        """Start loading ``backend_id`` in a background thread (idempotent).

        Returns the descriptor as of now; poll ``describe`` until
        ``load_state`` is ``loaded`` or ``failed``. Raises
        :class:`BackendNotLoadableError` when the backend reports a
        configuration reason that no load can fix.
        """
        backend = self.get(backend_id)
        state = self.load_state(backend_id)
        if state in ("loaded", "loading"):
            return self.describe(backend_id)
        loader = getattr(backend, "load", None)
        if not callable(loader):
            return self.describe(backend_id)
        reason_fn = getattr(backend, "unavailable_reason", None)
        reason = reason_fn() if callable(reason_fn) else backend.descriptor().unavailable_reason
        if reason is not None:
            raise BackendNotLoadableError(backend_id, reason)

        def run() -> None:
            try:
                loader()
                logger.info("backend loaded", extra={"backend_id": backend_id})
            except Exception as exc:  # noqa: BLE001 - the backend records the reason
                logger.warning(
                    "backend load failed",
                    extra={"backend_id": backend_id, "error": f"{type(exc).__name__}: {exc}"},
                )

        with self._lock:
            existing = self._loaders.get(backend_id)
            if existing is not None and existing.is_alive():
                return self.describe(backend_id)
            self._attempted.add(backend_id)
            thread = threading.Thread(target=run, name=f"afmlabel-load-{backend_id}", daemon=True)
            self._loaders[backend_id] = thread
            thread.start()
        return self.describe(backend_id)

    def wait_for_load(self, backend_id: str, timeout: float | None = None) -> str:
        with self._lock:
            thread = self._loaders.get(backend_id)
        if thread is not None:
            thread.join(timeout)
        return self.load_state(backend_id)

    # ------------------------------------------------------------ listing

    def list_descriptors(self) -> list[BackendDescriptor]:
        return [self.describe(backend_id) for backend_id in self._backends]

    def backend_ids(self) -> list[str]:
        return list(self._backends)

    def release_all(self) -> None:
        for backend in self._backends.values():
            backend.release()

    def __contains__(self, backend_id: str) -> bool:
        return backend_id in self._backends


def build_registry(settings: AppSettings) -> BackendRegistry:
    backends: dict[str, SegmentationBackend] = {
        "sam3": Sam3Backend(settings.models.sam3, settings.runtime),
        "sam31": Sam31Backend(settings.models.sam31, settings.runtime),
        "sam21": Sam21Backend(settings.models.sam21, settings.runtime),
        "fake": FakeBackend(),
    }
    return BackendRegistry(backends)
