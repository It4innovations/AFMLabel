#!/usr/bin/env python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Benchmark interactive inference of a local SAM backend on synthetic
AFM-like images (default backend ``sam31``; also ``sam3`` and ``sam21``).

    python scripts/benchmark_sam31.py --size 512 --iterations 10
    python scripts/benchmark_sam31.py --backend sam21 --size 512 --iterations 10

Reports load time, image-embedding time (first prompt on a new image),
prompt time (subsequent prompts on the same image), peak RSS and peak VRAM.
Uses the configured backend from config/app.yaml; exits 2 if unavailable.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import psutil

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from afmlabel.segmentation.base import BackendUnavailableError, PointPrompt  # noqa: E402
from afmlabel.segmentation.registry import build_registry  # noqa: E402
from afmlabel.settings import load_settings  # noqa: E402


def synthetic(size: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[:size, :size].astype(np.float64)
    cx, cy = rng.uniform(size * 0.3, size * 0.7, size=2)
    blob = np.exp(-(((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * (size / 8) ** 2)))
    return np.clip(blob * 45000 + xx * 20 + rng.normal(0, 300, (size, size)), 0, 65535).astype(
        np.uint16
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--backend", default="sam31")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--iterations", type=int, default=10)
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    registry = build_registry(settings)
    backend = registry.get(args.backend)
    process = psutil.Process()
    started = time.perf_counter()
    loader = getattr(backend, "load", None)
    try:
        if callable(loader):
            loader()
    except BackendUnavailableError as exc:
        print(f"unavailable: {exc.reason}")
        return 2
    load_seconds = time.perf_counter() - started
    descriptor = backend.descriptor()
    print(f"backend: {descriptor.backend_id} device={descriptor.device} dtype={descriptor.dtype}")
    print(f"load: {load_seconds:.2f}s")

    embed_times: list[float] = []
    prompt_times: list[float] = []
    for iteration in range(args.iterations):
        image = synthetic(args.size, iteration)
        center = args.size / 2
        t0 = time.perf_counter()
        backend.segment(
            image,
            points=[PointPrompt(center, center, True)],
            box_xyxy=None,
            previous_mask=None,
            text_prompt=None,
        )
        embed_times.append(time.perf_counter() - t0)
        t1 = time.perf_counter()
        backend.segment(
            image,
            points=[PointPrompt(center + 5, center - 5, True)],
            box_xyxy=None,
            previous_mask=None,
            text_prompt=None,
        )
        prompt_times.append(time.perf_counter() - t1)
    print(f"image size: {args.size}x{args.size}, iterations: {args.iterations}")
    print(
        "first prompt (embedding + decode): "
        f"mean {np.mean(embed_times):.3f}s, max {np.max(embed_times):.3f}s"
    )
    print(
        "follow-up prompt (decode only):    "
        f"mean {np.mean(prompt_times):.3f}s, max {np.max(prompt_times):.3f}s"
    )
    info = process.memory_info()
    peak = getattr(info, "peak_wset", info.rss)
    print(f"peak RSS: {peak / 1024**2:.0f} MiB")
    try:
        import torch

        if torch.cuda.is_available():
            print(f"peak VRAM: {torch.cuda.max_memory_allocated() / 1024**2:.0f} MiB")
    except ImportError:
        pass
    backend.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
