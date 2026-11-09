# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from pathlib import Path

import psutil
import pytest

from afmlabel.resources import monitor as monitor_module
from afmlabel.resources.monitor import (
    RESOURCE_METRIC_UNAVAILABLE,
    ResourceMonitor,
    probe_disk,
    probe_gpu,
)


def test_snapshot_reports_core_metrics(tmp_path: Path) -> None:
    monitor = ResourceMonitor(tmp_path, gpu_enabled=False)
    snapshot = monitor.snapshot()
    assert snapshot.memory.total_bytes and snapshot.memory.total_bytes > 0
    assert snapshot.memory.process_rss_bytes and snapshot.memory.process_rss_bytes > 0
    assert snapshot.disk.free_bytes is not None
    assert snapshot.cpu.logical_cores and snapshot.cpu.logical_cores > 0
    assert snapshot.gpu.source == "none"


def test_snapshot_is_cached_for_refresh_interval(tmp_path: Path) -> None:
    monitor = ResourceMonitor(tmp_path, refresh_seconds=60, gpu_enabled=False)
    first = monitor.snapshot()
    second = monitor.snapshot()
    assert first is second
    assert monitor.snapshot(force=True) is not first


def test_metric_failure_becomes_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> None:
        raise RuntimeError("no memory info here")

    monkeypatch.setattr(psutil, "virtual_memory", boom)
    monitor = ResourceMonitor(tmp_path, gpu_enabled=False)
    snapshot = monitor.snapshot(force=True)
    assert snapshot.memory.error is not None
    assert snapshot.memory.error.code == RESOURCE_METRIC_UNAVAILABLE
    assert any(w.code == RESOURCE_METRIC_UNAVAILABLE for w in snapshot.warnings)


def test_disk_probe_walks_up_to_existing_parent(tmp_path: Path) -> None:
    metrics = probe_disk(tmp_path / "does" / "not" / "exist")
    assert metrics.error is None
    assert metrics.free_bytes is not None


def test_low_disk_warning_uses_threshold(tmp_path: Path) -> None:
    monitor = ResourceMonitor(tmp_path, low_disk_warning_gb=10**9, gpu_enabled=False)
    snapshot = monitor.snapshot(force=True)
    assert any(w.code == "DISK_SPACE_LOW" for w in snapshot.warnings)


def test_gpu_probe_without_nvidia_smi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_module.shutil, "which", lambda _: None)
    assert probe_gpu().source == "none"


def test_gpu_probe_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stdout = "0, Fake GPU, 8192, 1024, 37\n"
        stderr = ""

    monkeypatch.setattr(monitor_module.shutil, "which", lambda _: "nvidia-smi")
    monkeypatch.setattr(monitor_module.subprocess, "run", lambda *a, **k: Result())
    metrics = probe_gpu()
    assert metrics.source == "nvidia-smi"
    assert metrics.devices[0].name == "Fake GPU"
    assert metrics.devices[0].memory_total_bytes == 8192 * 1024**2
