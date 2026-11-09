# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""NanoUNet export profile implemented from the verified
downstream loader contract (see the Export > Profiles section of
``docs/user-guide.md``).

Layout::

    Topography/<stem>.png         8-bit grayscale render (one folder per
    Friction/<stem>.png           project channel, named after its label)
    Masks_instance/<stem>.tif     uint16 single-channel mask, one value per pixel
                                  (``.png`` when raster_format=png)
    Masks_color/<stem>.png        RGB visualisation with the GUI class colours
                                  (not read by the loader; optional)
    manifest.json, classes.json

The downstream loader (``afm_sam_v1/v2.py``) reads exactly **one** mask per
sample from ``Masks_instance`` and matches the three directories by the
first digit run of the stem (``extract_id``). It never consumes a second
"instance" mask, so this profile writes the authoritative final composed
mask only; the richer instance artefacts stay in the native profile.
``<stem>`` is derived from the original filenames (pairing key), see
``exports/rasters.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image

from afmlabel.exports.base import DatasetExportEntry, ExactDataset, ExportContext
from afmlabel.exports.native_exact import entry_for
from afmlabel.exports.rasters import raster_suffix, write_color_mask, write_label_raster
from afmlabel.images.decode import decode_image
from afmlabel.images.display import DEFAULT_DISPLAY, to_display_uint8

PROFILE_ID = "nanounet"

IMAGE_DIRS = ("Topography", "Friction")
MASK_DIR = "Masks_instance"
COLOR_DIR = "Masks_color"


def channel_folder(channel_key: str, label: str | None = None) -> str:
    """Export folder for a channel: ``Topography`` / ``Friction`` for the classic
    two, otherwise the channel label with filesystem-unsafe characters replaced."""
    if channel_key == "topography":
        return "Topography"
    if channel_key == "friction":
        return "Friction"
    text = (label or channel_key).strip()
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")
    return cleaned or channel_key


def write_uint16_tiff(path: Path, array: np.ndarray) -> None:
    """Kept for existing callers; delegates to the shared writer."""
    write_label_raster(path, array, "tiff")


def write_gray_png(path: Path, source: Path) -> None:
    decoded = decode_image(source)
    display = to_display_uint8(decoded.array, DEFAULT_DISPLAY)
    if display.ndim == 3:
        # Colour renders are converted to luminance so the downstream
        # ``convert("RGB")`` sees a neutral grayscale image.
        display = (
            (0.2126 * display[:, :, 0] + 0.7152 * display[:, :, 1] + 0.0722 * display[:, :, 2])
            .round()
            .astype(np.uint8)
        )
    Image.fromarray(display, mode="L").save(path, format="PNG")


class NanoUNetExport:
    profile_id = PROFILE_ID
    display_name = "NanoUNet (Topography/Friction/Masks_instance)"
    #: The loader matches samples by the first digit run of the stem.
    loader_id_matching = True

    def write(
        self, root: Path, context: ExportContext, datasets: list[ExactDataset]
    ) -> list[DatasetExportEntry]:
        options = context.options
        folders: dict[str, str] = {}
        for exact in datasets:
            for channel in exact.dataset.channel_keys:
                folders.setdefault(
                    channel, context.channel_folders.get(channel) or channel_folder(channel)
                )
        for sub in (*folders.values(), MASK_DIR):
            (root / sub).mkdir(parents=True, exist_ok=True)
        if options.color_visualization:
            (root / COLOR_DIR).mkdir(parents=True, exist_ok=True)
        entries: list[DatasetExportEntry] = []
        suffix = raster_suffix(options.raster_format)
        for exact in datasets:
            stem = exact.export_stem
            files: dict[str, str] = {}
            for channel in exact.dataset.channel_keys:
                folder = folders[channel]
                source = context.source_paths[(exact.dataset.dataset_id, channel)]
                target = root / folder / f"{stem}.png"
                write_gray_png(target, source)
                files[channel] = f"{folder}/{stem}.png"
            mask = exact.instance_map if options.mask_semantics == "instance" else exact.label_map
            mask_path = root / MASK_DIR / f"{stem}{suffix}"
            write_label_raster(mask_path, mask, options.raster_format)
            files["mask"] = f"{MASK_DIR}/{stem}{suffix}"
            if options.color_visualization:
                color_path = root / COLOR_DIR / f"{stem}.png"
                # Always from the class label map: the visualisation shows
                # classes even when the machine mask carries instance ids.
                write_color_mask(color_path, exact.label_map, context.color_legend)
                files["mask_color"] = f"{COLOR_DIR}/{stem}.png"
            entry = entry_for(exact, files)
            entry.warnings.append(
                "images are 8-bit percentile-stretched renders (downstream loader uses "
                "PIL convert('RGB')); the manifest records the normalisation parameters"
            )
            entries.append(entry)
        return entries
