# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Append-only JSON Lines log (also used for the audit events)."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class JsonlLog:
    def __init__(self, path: Path, *, fsync: bool = False) -> None:
        self._path = path
        self._fsync = fsync
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                if self._fsync:
                    os.fsync(handle.fileno())

    def iter_records(self) -> Iterator[dict[str, Any]]:
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A torn final line from a crash mid-append is skipped,
                    # never fatal; earlier complete records stay readable.
                    continue
                if isinstance(record, dict):
                    yield record

    def tail(self, limit: int) -> list[dict[str, Any]]:
        records = list(self.iter_records())
        return records[-limit:] if limit > 0 else records
