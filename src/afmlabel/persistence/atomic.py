# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Atomic file writes and Pydantic (de)serialisation helpers.

Write sequence: serialise to a temporary file in the *same* directory,
flush, optionally ``fsync``, then ``os.replace`` onto the destination. A
crash at any point before the final replace leaves the previous file
untouched: there is never a partially written authoritative JSON file.

Windows rename semantics are stricter than POSIX: a reader that has the
destination open can make ``os.replace`` raise ``PermissionError``, and a
reader can transiently fail to open a path while a replace is in flight.
Both directions are retried a bounded number of times.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_RETRY_ATTEMPTS = 8
_RETRY_DELAY_SECONDS = 0.02

TEMP_SUFFIX = ".tmp"


def _read_text_with_retry(path: Path) -> str:
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")  # pragma: no cover


def _replace_with_retry(tmp_name: str, path: Path) -> None:
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            os.replace(tmp_name, path)
            return
        except PermissionError:
            if attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_DELAY_SECONDS)


def replace_with_retry(
    source: Path, destination: Path, *, attempts: int = 12, first_delay: float = 0.02
) -> None:
    """``os.replace`` for files *or directories* with exponential back-off.

    On Windows a directory that was just written can be held briefly by an
    indexer or virus scanner, which turns the rename into a transient
    ``PermissionError`` (WinError 5/32). Publishing hundreds of dataset
    directories in one commit hits this reliably, so the rename is retried
    (about 40 s in total at the defaults) before giving up.
    """
    delay = first_delay
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 8.0)
    raise AssertionError("unreachable")  # pragma: no cover


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=TEMP_SUFFIX)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        _replace_with_retry(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_text(path: Path, text: str, *, fsync: bool = True) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), fsync=fsync)


def atomic_write_json(path: Path, data: dict[str, Any], *, fsync: bool = True) -> None:
    atomic_write_text(path, json.dumps(data, indent=2), fsync=fsync)


def save_model(path: Path, model: BaseModel, *, fsync: bool = True) -> None:
    atomic_write_text(path, model.model_dump_json(indent=2), fsync=fsync)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(_read_text_with_retry(path))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return data


def load_model[T: BaseModel](path: Path, model_cls: type[T]) -> T:
    return model_cls.model_validate_json(_read_text_with_retry(path))


def remove_stale_temp_files(directory: Path) -> list[Path]:
    """Delete leftover ``.<name>.*.tmp`` files from interrupted writes."""
    removed: list[Path] = []
    if not directory.exists():
        return removed
    for candidate in directory.rglob(f".*{TEMP_SUFFIX}"):
        if candidate.is_file():
            with contextlib.suppress(OSError):
                candidate.unlink()
                removed.append(candidate)
    return removed
