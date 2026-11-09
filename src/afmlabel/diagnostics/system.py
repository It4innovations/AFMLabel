# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Startup/health diagnostics used by ``GET /api/v1/health``.

Reports Python, filesystem, RAM, disk and optional GPU status. PyTorch is
an optional extra: a missing ``torch`` is reported, never raised, and it is
detected via ``importlib.util.find_spec`` so the health endpoint never pays
the cost of importing it.
"""

from __future__ import annotations

import importlib.util
import platform
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from afmlabel import __version__
from afmlabel.resources.monitor import ResourceSnapshot
from afmlabel.settings import AppSettings

SUPPORTED_PYTHON_MINORS: tuple[tuple[int, int], ...] = ((3, 12), (3, 13))

Status = Literal["ok", "degraded", "missing", "error"]


class CheckResult(BaseModel):
    status: Status
    detail: str


class SystemDiagnosticsReport(BaseModel):
    status: Status
    package_version: str
    platform: str
    python: CheckResult
    filesystem: CheckResult
    memory: CheckResult
    disk: CheckResult
    gpu: CheckResult
    torch: CheckResult


def check_python() -> CheckResult:
    major, minor = sys.version_info[:2]
    detail = f"{sys.version.split()[0]} ({sys.executable})"
    if (major, minor) in SUPPORTED_PYTHON_MINORS:
        return CheckResult(status="ok", detail=detail)
    return CheckResult(
        status="degraded",
        detail=f"{detail} -- outside the supported range 3.12-3.13; SAM 3 code requires >= 3.12",
    )


def check_filesystem(workspace_root: Path) -> CheckResult:
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
        probe = workspace_root / ".afmlabel_write_check"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return CheckResult(status="error", detail=f"workspace root not writable: {exc}")
    return CheckResult(status="ok", detail=f"workspace root writable: {workspace_root.resolve()}")


def check_memory(snapshot: ResourceSnapshot) -> CheckResult:
    memory = snapshot.memory
    if memory.error is not None or memory.total_bytes is None or memory.available_bytes is None:
        return CheckResult(
            status="degraded", detail=memory.error.message if memory.error else "unavailable"
        )
    total_gb = memory.total_bytes / 1024**3
    available_gb = memory.available_bytes / 1024**3
    status: Status = "ok"
    if any(w.code == "RAM_LOW" for w in snapshot.warnings):
        status = "degraded"
    return CheckResult(
        status=status, detail=f"{available_gb:.1f} GB available of {total_gb:.1f} GB"
    )


def check_disk(snapshot: ResourceSnapshot, minimum_free_gb: float) -> CheckResult:
    disk = snapshot.disk
    if disk.error is not None or disk.free_bytes is None:
        return CheckResult(
            status="degraded", detail=disk.error.message if disk.error else "unavailable"
        )
    free_gb = disk.free_bytes / 1024**3
    if free_gb < minimum_free_gb:
        return CheckResult(
            status="degraded",
            detail=f"{free_gb:.1f} GB free at {disk.path} (minimum {minimum_free_gb:.1f} GB)",
        )
    return CheckResult(status="ok", detail=f"{free_gb:.1f} GB free at {disk.path}")


def check_gpu(snapshot: ResourceSnapshot) -> CheckResult:
    gpu = snapshot.gpu
    if gpu.error is not None:
        return CheckResult(status="degraded", detail=gpu.error.message)
    if gpu.source == "none" or not gpu.devices:
        return CheckResult(status="missing", detail="no GPU detected (CPU-only profile)")
    names = ", ".join(f"{d.index}: {d.name}" for d in gpu.devices)
    return CheckResult(
        status="ok", detail=f"{len(gpu.devices)} device(s) via {gpu.source}: {names}"
    )


def check_torch() -> CheckResult:
    if importlib.util.find_spec("torch") is None:
        return CheckResult(
            status="missing",
            detail="torch not installed (optional 'sam' extra; the core app does not need it)",
        )
    return CheckResult(status="ok", detail="torch importable (not loaded by the health check)")


def _overall_status(results: list[CheckResult]) -> Status:
    if any(result.status == "error" for result in results):
        return "error"
    if any(result.status == "degraded" for result in results):
        return "degraded"
    return "ok"


def run_system_diagnostics(
    settings: AppSettings, snapshot: ResourceSnapshot
) -> SystemDiagnosticsReport:
    python = check_python()
    filesystem = check_filesystem(settings.workspace.root)
    memory = check_memory(snapshot)
    disk = check_disk(snapshot, settings.workspace.minimum_free_disk_gb)
    gpu = check_gpu(snapshot)
    torch = check_torch()
    # A "missing" GPU/torch is the normal CPU-only profile, not degradation.
    return SystemDiagnosticsReport(
        status=_overall_status([python, filesystem, memory, disk]),
        package_version=__version__,
        platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=python,
        filesystem=filesystem,
        memory=memory,
        disk=disk,
        gpu=gpu,
        torch=torch,
    )


if __name__ == "__main__":  # pragma: no cover - manual convenience entry point
    from afmlabel.resources.monitor import ResourceMonitor
    from afmlabel.settings import load_settings

    loaded = load_settings()
    monitor = ResourceMonitor(loaded.workspace.root)
    report = run_system_diagnostics(loaded, monitor.snapshot())
    print(report.model_dump_json(indent=2))
    sys.exit(0 if report.status != "error" else 1)
