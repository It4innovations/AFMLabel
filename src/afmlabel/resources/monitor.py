# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""CPU / RAM / disk / process / optional GPU snapshot.

Every metric is collected independently; a failing probe becomes a typed
``RESOURCE_METRIC_UNAVAILABLE`` entry instead of an exception so the editor
keeps working when, say, ``nvidia-smi`` hangs or a network filesystem
refuses ``statvfs``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Literal

import psutil
from pydantic import BaseModel, Field

RESOURCE_METRIC_UNAVAILABLE = "RESOURCE_METRIC_UNAVAILABLE"
DISK_SPACE_LOW = "DISK_SPACE_LOW"
RAM_LOW = "RAM_LOW"

WarningLevel = Literal["warning", "critical"]


class MetricError(BaseModel):
    code: str = RESOURCE_METRIC_UNAVAILABLE
    message: str


class CpuMetrics(BaseModel):
    host_percent: float | None = None
    process_percent: float | None = None
    logical_cores: int | None = None
    error: MetricError | None = None


class MemoryMetrics(BaseModel):
    total_bytes: int | None = None
    available_bytes: int | None = None
    used_percent: float | None = None
    process_rss_bytes: int | None = None
    error: MetricError | None = None


class DiskMetrics(BaseModel):
    path: str
    total_bytes: int | None = None
    free_bytes: int | None = None
    used_percent: float | None = None
    error: MetricError | None = None


class GpuDevice(BaseModel):
    index: int
    name: str
    memory_total_bytes: int | None = None
    memory_used_bytes: int | None = None
    utilization_percent: float | None = None


class GpuMetrics(BaseModel):
    #: "nvidia-smi" | "none" | "unavailable"
    source: str
    devices: list[GpuDevice] = Field(default_factory=list)
    error: MetricError | None = None


class ResourceAlert(BaseModel):
    code: str
    level: WarningLevel
    message: str


class ResourceSnapshot(BaseModel):
    collected_at: float
    cpu: CpuMetrics
    memory: MemoryMetrics
    disk: DiskMetrics
    gpu: GpuMetrics
    warnings: list[ResourceAlert] = Field(default_factory=list)


def _bytes_to_gb(value: int) -> float:
    return value / 1024**3


def probe_cpu(process: psutil.Process) -> CpuMetrics:
    try:
        return CpuMetrics(
            host_percent=psutil.cpu_percent(interval=None),
            process_percent=process.cpu_percent(interval=None),
            logical_cores=psutil.cpu_count(logical=True),
        )
    except Exception as exc:  # noqa: BLE001 - metric failure must never propagate
        return CpuMetrics(error=MetricError(message=f"cpu probe failed: {exc}"))


def probe_memory(process: psutil.Process) -> MemoryMetrics:
    try:
        vm = psutil.virtual_memory()
        return MemoryMetrics(
            total_bytes=int(vm.total),
            available_bytes=int(vm.available),
            used_percent=float(vm.percent),
            process_rss_bytes=int(process.memory_info().rss),
        )
    except Exception as exc:  # noqa: BLE001
        return MemoryMetrics(error=MetricError(message=f"memory probe failed: {exc}"))


def probe_disk(path: Path) -> DiskMetrics:
    try:
        target = path.resolve()
        while not target.exists() and target.parent != target:
            target = target.parent
        usage = shutil.disk_usage(target)
        used_percent = (usage.used / usage.total * 100.0) if usage.total else None
        return DiskMetrics(
            path=str(path),
            total_bytes=int(usage.total),
            free_bytes=int(usage.free),
            used_percent=used_percent,
        )
    except Exception as exc:  # noqa: BLE001
        return DiskMetrics(path=str(path), error=MetricError(message=f"disk probe failed: {exc}"))


def probe_gpu(timeout_seconds: float = 2.0) -> GpuMetrics:
    """Query ``nvidia-smi`` if present. No PyTorch import: importing torch is
    slow and must not happen on every 2-second refresh. ROCm hosts report
    ``source="none"`` until a validated probe exists."""
    executable = shutil.which("nvidia-smi")
    if executable is None:
        return GpuMetrics(source="none")
    try:
        result = subprocess.run(
            [
                executable,
                "--query-gpu=index,name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return GpuMetrics(
            source="unavailable", error=MetricError(message=f"nvidia-smi failed: {exc}")
        )
    if result.returncode != 0:
        return GpuMetrics(
            source="unavailable",
            error=MetricError(
                message=f"nvidia-smi exited with {result.returncode}: {result.stderr.strip()[:200]}"
            ),
        )
    devices: list[GpuDevice] = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            devices.append(
                GpuDevice(
                    index=int(parts[0]),
                    name=parts[1],
                    memory_total_bytes=int(float(parts[2])) * 1024**2,
                    memory_used_bytes=int(float(parts[3])) * 1024**2,
                    utilization_percent=float(parts[4]),
                )
            )
        except ValueError:
            continue
    return GpuMetrics(source="nvidia-smi", devices=devices)


class ResourceMonitor:
    """Caches snapshots for ``refresh_seconds`` so many browser tabs polling
    ``/api/v1/system/resources`` do not multiply probe cost."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        refresh_seconds: float = 2.0,
        low_ram_warning_gb: float = 2.0,
        low_disk_warning_gb: float = 10.0,
        gpu_enabled: bool = True,
    ) -> None:
        self._workspace_root = workspace_root
        self._refresh_seconds = refresh_seconds
        self._low_ram_warning_gb = low_ram_warning_gb
        self._low_disk_warning_gb = low_disk_warning_gb
        self._gpu_enabled = gpu_enabled
        self._process = psutil.Process(os.getpid())
        self._lock = threading.Lock()
        self._cached: ResourceSnapshot | None = None
        self._cached_at = 0.0
        # Prime the CPU counters so the first real reading is meaningful.
        try:
            psutil.cpu_percent(interval=None)
            self._process.cpu_percent(interval=None)
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self, *, force: bool = False) -> ResourceSnapshot:
        now = time.monotonic()
        with self._lock:
            if (
                not force
                and self._cached is not None
                and now - self._cached_at < self._refresh_seconds
            ):
                return self._cached
            snapshot = self.collect()
            self._cached = snapshot
            self._cached_at = now
            return snapshot

    def collect(self) -> ResourceSnapshot:
        cpu = probe_cpu(self._process)
        memory = probe_memory(self._process)
        disk = probe_disk(self._workspace_root)
        gpu = probe_gpu() if self._gpu_enabled else GpuMetrics(source="none")
        warnings: list[ResourceAlert] = []
        if memory.available_bytes is not None:
            available_gb = _bytes_to_gb(memory.available_bytes)
            if available_gb < self._low_ram_warning_gb:
                warnings.append(
                    ResourceAlert(
                        code=RAM_LOW,
                        level="warning",
                        message=(
                            f"available RAM {available_gb:.1f} GB is below the "
                            f"{self._low_ram_warning_gb:.1f} GB threshold"
                        ),
                    )
                )
        if disk.free_bytes is not None:
            free_gb = _bytes_to_gb(disk.free_bytes)
            if free_gb < self._low_disk_warning_gb:
                warnings.append(
                    ResourceAlert(
                        code=DISK_SPACE_LOW,
                        level="warning",
                        message=(
                            f"free workspace disk space {free_gb:.1f} GB is below the "
                            f"{self._low_disk_warning_gb:.1f} GB threshold"
                        ),
                    )
                )
        for metric in (cpu.error, memory.error, disk.error, gpu.error):
            if metric is not None:
                warnings.append(
                    ResourceAlert(code=metric.code, level="warning", message=metric.message)
                )
        return ResourceSnapshot(
            collected_at=time.time(), cpu=cpu, memory=memory, disk=disk, gpu=gpu, warnings=warnings
        )
