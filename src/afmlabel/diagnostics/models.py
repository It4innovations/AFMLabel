# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Model backend diagnostic: ``python -m afmlabel.diagnostics.models --backend sam31``.

A backend is reported usable only after a real inference on a
synthetic image succeeds and every advertised prompt capability has been
exercised. Exit status: ``0`` usable, ``2`` unavailable (with the exact
reason), ``1`` failed a diagnostic step.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Any, Literal

import numpy as np
import psutil
from pydantic import BaseModel, Field

from afmlabel.segmentation.base import (
    BackendCapability,
    BackendCapabilityError,
    BackendUnavailableError,
    PointPrompt,
    SegmentationBackend,
    SegmentationProposal,
)
from afmlabel.segmentation.registry import build_registry
from afmlabel.settings import load_settings

StepStatus = Literal["ok", "skipped", "failed"]
ReportStatus = Literal["ok", "unavailable", "failed"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_UNAVAILABLE = 2


class DiagnosticStep(BaseModel):
    name: str
    status: StepStatus
    detail: str
    seconds: float | None = None


class DiagnosticReport(BaseModel):
    backend_id: str
    status: ReportStatus
    reason: str | None = None
    display_name: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    verified_capabilities: list[str] = Field(default_factory=list)
    model_repository: str | None = None
    model_revision: str | None = None
    checkpoint_sha256: str | None = None
    code_revision: str | None = None
    device: str | None = None
    dtype: str | None = None
    peak_rss_bytes: int | None = None
    peak_vram_bytes: int | None = None
    warnings: list[str] = Field(default_factory=list)
    steps: list[DiagnosticStep] = Field(default_factory=list)

    @property
    def exit_code(self) -> int:
        if self.status == "ok":
            return EXIT_OK
        if self.status == "unavailable":
            return EXIT_UNAVAILABLE
        return EXIT_FAILED


def synthetic_afm_image(height: int = 96, width: int = 96) -> np.ndarray:
    """Deterministic uint16 gradient with a bright blob -- looks vaguely like
    a topography channel and is enough to exercise a backend end to end."""
    yy, xx = np.mgrid[:height, :width].astype(np.float64)
    base = (xx / max(width - 1, 1)) * 20000.0 + (yy / max(height - 1, 1)) * 10000.0
    blob = 30000.0 * np.exp(-(((xx - width / 2) ** 2 + (yy - height / 2) ** 2) / (2 * 12.0**2)))
    return np.clip(base + blob, 0, 65535).astype(np.uint16)


def _peak_rss_bytes(process: psutil.Process) -> int | None:
    info = process.memory_info()
    peak = getattr(info, "peak_wset", None)
    if peak is not None:
        return int(peak)
    try:
        resource_module: Any = importlib.import_module("resource")
        ru = resource_module.getrusage(resource_module.RUSAGE_SELF)
        # ru_maxrss is kilobytes on Linux, bytes on macOS.
        multiplier = 1 if sys.platform == "darwin" else 1024
        return int(ru.ru_maxrss) * multiplier
    except (ImportError, OSError, AttributeError):
        return int(info.rss)


def _peak_vram_bytes(device: str | None) -> int | None:
    if not device or not device.startswith("cuda"):
        return None
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        return int(torch.cuda.max_memory_allocated())
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def _check_proposals(
    proposals: list[SegmentationProposal], shape: tuple[int, int], *, allow_empty: bool = False
) -> str:
    if not proposals:
        if allow_empty:
            return "0 proposal(s) (no instance matched the prompt; typed empty result)"
        raise AssertionError("backend returned no proposals")
    for index, proposal in enumerate(proposals):
        if proposal.mask.shape != shape:
            raise AssertionError(
                f"proposal {index} mask shape {proposal.mask.shape} != image shape {shape}"
            )
        if proposal.mask.dtype != np.bool_:
            raise AssertionError(f"proposal {index} mask dtype {proposal.mask.dtype} is not bool")
    return f"{len(proposals)} proposal(s), first covers {int(proposals[0].mask.sum())} px"


def run_backend_diagnostic(backend: SegmentationBackend) -> DiagnosticReport:
    process = psutil.Process()
    report = DiagnosticReport(backend_id=backend.backend_id, status="ok")

    def step(name: str, status: StepStatus, detail: str, started: float | None = None) -> None:
        report.steps.append(
            DiagnosticStep(
                name=name,
                status=status,
                detail=detail,
                seconds=round(time.perf_counter() - started, 3) if started else None,
            )
        )

    # 1-5: import, locate checkpoint, verify manifest, detect device, load.
    loader = getattr(backend, "load", None)
    started = time.perf_counter()
    if callable(loader):
        try:
            loader()
            step("load", "ok", "model loaded", started)
        except BackendUnavailableError as exc:
            step("load", "failed", exc.reason, started)
            report.status = "unavailable"
            report.reason = exc.reason
        except MemoryError as exc:
            step("load", "failed", f"out of memory: {exc}", started)
            report.status = "failed"
            report.reason = f"MODEL_OOM: {exc}"
        except Exception as exc:  # noqa: BLE001 - diagnostics must report, not crash
            step("load", "failed", f"{type(exc).__name__}: {exc}", started)
            report.status = "failed"
            report.reason = str(exc)
    descriptor = backend.descriptor()
    report.display_name = descriptor.display_name
    report.capabilities = sorted(c.value for c in descriptor.capabilities)
    report.model_repository = descriptor.model_repository
    report.model_revision = descriptor.model_revision
    report.checkpoint_sha256 = descriptor.checkpoint_sha256
    report.code_revision = descriptor.code_revision
    report.device = descriptor.device
    report.dtype = descriptor.dtype
    report.warnings = list(descriptor.warnings)
    if not descriptor.available:
        if report.status == "ok":
            report.status = "unavailable"
            report.reason = descriptor.unavailable_reason
        step("descriptor", "failed", descriptor.unavailable_reason or "unavailable")
        report.peak_rss_bytes = _peak_rss_bytes(process)
        return report
    step("descriptor", "ok", f"{descriptor.display_name}; capabilities={report.capabilities}")

    image = synthetic_afm_image()
    shape = (image.shape[0], image.shape[1])
    cx, cy = image.shape[1] / 2, image.shape[0] / 2
    previous: np.ndarray | None = None

    def exercise(
        name: str,
        capability: BackendCapability,
        *,
        points: list[PointPrompt],
        box: tuple[float, float, float, float] | None,
        mask: np.ndarray | None,
        text: str | None,
    ) -> np.ndarray | None:
        if capability not in descriptor.capabilities:
            step(name, "skipped", f"{capability.value} not advertised")
            return None
        started = time.perf_counter()
        try:
            proposals = backend.segment(
                image, points=points, box_xyxy=box, previous_mask=mask, text_prompt=text
            )
            detail = _check_proposals(
                proposals, shape, allow_empty=capability == BackendCapability.TEXT_PROMPTS
            )
        except BackendCapabilityError as exc:
            step(name, "failed", f"advertised but rejected: {exc.reason}", started)
            report.status = "failed"
            return None
        except MemoryError as exc:
            step(name, "failed", f"MODEL_OOM: {exc}", started)
            report.status = "failed"
            return None
        except Exception as exc:  # noqa: BLE001
            step(name, "failed", f"{type(exc).__name__}: {exc}", started)
            report.status = "failed"
            return None
        step(name, "ok", detail, started)
        report.verified_capabilities.append(capability.value)
        return proposals[0].mask if proposals else None

    previous = exercise(
        "point_prompt",
        BackendCapability.POINT_PROMPTS,
        points=[PointPrompt(cx, cy, True), PointPrompt(cx + 30, cy + 30, False)],
        box=None,
        mask=None,
        text=None,
    )
    exercise(
        "box_prompt",
        BackendCapability.BOX_PROMPTS,
        points=[],
        box=(cx - 15, cy - 15, cx + 15, cy + 15),
        mask=None,
        text=None,
    )
    exercise(
        "mask_prompt",
        BackendCapability.MASK_PROMPTS,
        points=[PointPrompt(cx, cy, True)],
        box=None,
        mask=previous if previous is not None else np.zeros(shape, dtype=bool),
        text=None,
    )
    exercise(
        "text_prompt",
        BackendCapability.TEXT_PROMPTS,
        points=[],
        box=None,
        mask=None,
        text="bright blob",
    )

    # Unsupported prompt kinds must be rejected with the typed error.
    unsupported = [c for c in (BackendCapability.TEXT_PROMPTS,) if c not in descriptor.capabilities]
    for capability in unsupported:
        try:
            backend.segment(image, points=[], box_xyxy=None, previous_mask=None, text_prompt="x")
        except BackendCapabilityError:
            step("typed_rejection", "ok", f"{capability.value} rejected with typed error")
        except Exception as exc:  # noqa: BLE001
            step("typed_rejection", "failed", f"wrong error type {type(exc).__name__}: {exc}")
            report.status = "failed"
        else:
            step("typed_rejection", "failed", f"{capability.value} silently accepted")
            report.status = "failed"

    report.peak_rss_bytes = _peak_rss_bytes(process)
    report.peak_vram_bytes = _peak_vram_bytes(descriptor.device)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m afmlabel.diagnostics.models")
    parser.add_argument("--backend", required=True, help="backend id, e.g. fake or sam31")
    parser.add_argument("--config", type=Path, default=None, help="path to config/app.yaml")
    parser.add_argument("--json", action="store_true", help="print the report as JSON only")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    registry = build_registry(settings)
    if args.backend not in registry:
        print(f"unknown backend {args.backend!r}; known: {registry.backend_ids()}", file=sys.stderr)
        return EXIT_FAILED
    report = run_backend_diagnostic(registry.get(args.backend))
    if args.json:
        print(report.model_dump_json(indent=2))
    else:
        print(f"backend:   {report.backend_id} ({report.display_name})")
        print(f"status:    {report.status}" + (f" -- {report.reason}" if report.reason else ""))
        print(f"device:    {report.device}  dtype: {report.dtype}")
        print(f"model:     {report.model_repository} @ {report.model_revision}")
        print(f"checkpoint sha256: {report.checkpoint_sha256}")
        print(f"code revision:     {report.code_revision}")
        print(f"verified capabilities: {report.verified_capabilities}")
        for warning in report.warnings:
            print(f"  note: {warning}")
        if report.peak_rss_bytes is not None:
            print(f"peak RSS:  {report.peak_rss_bytes / 1024**2:.1f} MiB")
        if report.peak_vram_bytes is not None:
            print(f"peak VRAM: {report.peak_vram_bytes / 1024**2:.1f} MiB")
        for item in report.steps:
            timing = f" ({item.seconds}s)" if item.seconds is not None else ""
            print(f"  [{item.status:^7}] {item.name}: {item.detail}{timing}")
    registry.release_all()
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
