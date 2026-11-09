# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Faithful copy of the NanoUNet loader contract (numpy/Pillow only).

Reproduced from ``scripts/afm_sam_v2.py`` at revision
cce5416e23035d367480ec68fc3c471bc5b183bd so exports can be verified against
the real loading behaviour without torch or the downstream repository.
"""

from __future__ import annotations

import glob
import os
import re

import numpy as np
from PIL import Image

BOUNDARY_WIDTH = 2


def list_images(directory: str) -> list[str]:
    exts = ["*.tif", "*.tiff", "*.png", "*.jpg", "*.jpeg"]
    files: list[str] = []
    for ext in exts:
        files += glob.glob(os.path.join(directory, ext))
    return sorted(files)


def extract_id(path: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    match = re.search(r"\d+", stem)
    return match.group(0) if match else stem


def build_file_triplets(topo_dir: str, fric_dir: str, mask_dir: str) -> list[tuple[str, str, str]]:
    topo_map = {extract_id(f): f for f in list_images(topo_dir)}
    fric_map = {extract_id(f): f for f in list_images(fric_dir)}
    mask_map = {extract_id(f): f for f in list_images(mask_dir)}
    common = sorted(set(topo_map) & set(fric_map) & set(mask_map))
    triplets = [(topo_map[k], fric_map[k], mask_map[k]) for k in common]
    if len(triplets) == 0:
        raise RuntimeError("No matched triplets found.")
    return triplets


def rgb_to_gray_float(rgb_array: np.ndarray) -> np.ndarray:
    r = rgb_array[:, :, 0].astype(np.float32)
    g = rgb_array[:, :, 1].astype(np.float32)
    b = rgb_array[:, :, 2].astype(np.float32)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def load_input_image(topo_path: str, fric_path: str) -> np.ndarray:
    topo_rgb = np.array(Image.open(topo_path).convert("RGB"))
    topo_gray = rgb_to_gray_float(topo_rgb)
    fric_rgb = np.array(Image.open(fric_path).convert("RGB")).astype(np.float32) / 255.0
    return np.concatenate([topo_gray[:, :, None], fric_rgb], axis=2)


def load_instance_mask(mask_path: str) -> np.ndarray:
    img = Image.open(mask_path)
    mask = np.array(img)
    if mask.ndim == 3:
        mask = mask[:, :, 0]
    return mask.astype(np.int32)


def instance_to_targets(
    instance_map: np.ndarray, boundary_width: int = BOUNDARY_WIDTH
) -> tuple[np.ndarray, np.ndarray]:
    h, w = instance_map.shape
    boundary = np.zeros((h, w), dtype=bool)
    boundary[:-1, :] |= instance_map[:-1, :] != instance_map[1:, :]
    boundary[:, :-1] |= instance_map[:, :-1] != instance_map[:, 1:]
    # The original dilates with skimage; a 3x3 max filter reproduces disk(1).
    if boundary_width > 1:
        padded = np.pad(boundary, 1)
        boundary = np.zeros_like(boundary)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if abs(dy) + abs(dx) <= 1:
                    boundary |= padded[1 + dy : h + 1 + dy, 1 + dx : w + 1 + dx]
    interior = ~boundary
    return interior.astype(np.float32), boundary.astype(np.float32)
