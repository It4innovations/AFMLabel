# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Project editing lock with heartbeat and stale-lock recovery.

One authoritative editing session per project. The lock is a small JSON
file created with ``O_EXCL``; the holder refreshes ``heartbeat_at``
periodically. A lock whose heartbeat is older than ``stale_after_seconds``
may be taken over by anyone; a live lock held by another editor can only be
taken over explicitly (``force=True``), which the API records in the audit
log. The lock is a courtesy mechanism -- optimistic revisions remain the
real conflict guard ("Do not rely only on a lock").
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
from datetime import datetime
from pathlib import Path

from afmlabel.domain.common import utc_now, utc_now_iso
from afmlabel.domain.project import LockInfo, LockState
from afmlabel.errors import ConflictError
from afmlabel.persistence.atomic import atomic_write_text


class ProjectLockedError(ConflictError):
    def __init__(self, project_id: str, holder: LockInfo) -> None:
        super().__init__(
            "PROJECT_LOCKED",
            f"project {project_id} is being edited by another session "
            f"({holder.owner_label}, heartbeat {holder.heartbeat_at})",
            target={"project_id": project_id},
            recovery="open the project read-only, or take over the lock explicitly",
            details={"holder": holder.model_dump()},
        )


class ProjectLock:
    def __init__(self, project_id: str, path: Path, *, stale_after_seconds: float = 90.0) -> None:
        self._project_id = project_id
        self._path = path
        self._stale_after = stale_after_seconds
        self._guard = threading.Lock()

    # ---------------------------------------------------------------- reads

    def read(self) -> LockInfo | None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Torn/corrupt lock file: treat as stale so it can be recovered.
            return None
        try:
            return LockInfo.model_validate(raw)
        except ValueError:
            return None

    def is_stale(self, info: LockInfo) -> bool:
        try:
            heartbeat = datetime.fromisoformat(info.heartbeat_at)
        except ValueError:
            return True
        return (utc_now() - heartbeat).total_seconds() > self._stale_after

    def status(self, owner_id: str | None) -> LockState:
        info = self.read()
        if info is None:
            return LockState(locked=False, owned_by_caller=False, stale=False, info=None)
        return LockState(
            locked=True,
            owned_by_caller=owner_id is not None and info.owner_id == owner_id,
            stale=self.is_stale(info),
            info=info,
        )

    # --------------------------------------------------------------- writes

    def acquire(self, owner_id: str, owner_label: str, *, force: bool = False) -> LockInfo:
        with self._guard:
            existing = self.read()
            if existing is not None and existing.owner_id != owner_id:
                if not force and not self.is_stale(existing):
                    raise ProjectLockedError(self._project_id, existing)
                # Stale or forced takeover: fall through and replace.
            now = utc_now_iso()
            info = LockInfo(
                owner_id=owner_id,
                owner_label=owner_label,
                acquired_at=existing.acquired_at
                if existing is not None and existing.owner_id == owner_id
                else now,
                heartbeat_at=now,
                pid=os.getpid(),
                hostname=socket.gethostname(),
            )
            if existing is None:
                self._create_exclusive(info)
            else:
                atomic_write_text(self._path, info.model_dump_json(indent=2), fsync=False)
            return info

    def _create_exclusive(self, info: LockInfo) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            existing = self.read()
            if existing is not None and existing.owner_id != info.owner_id:
                if not self.is_stale(existing):
                    raise ProjectLockedError(self._project_id, existing) from None
            atomic_write_text(self._path, info.model_dump_json(indent=2), fsync=False)
            return
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(info.model_dump_json(indent=2))

    def heartbeat(self, owner_id: str) -> LockInfo:
        with self._guard:
            existing = self.read()
            if existing is None or existing.owner_id != owner_id:
                if existing is not None:
                    raise ProjectLockedError(self._project_id, existing)
                raise ConflictError(
                    "PROJECT_LOCKED",
                    f"project {self._project_id} lock is not held by this session",
                    target={"project_id": self._project_id},
                    recovery="re-acquire the lock",
                )
            refreshed = existing.model_copy(update={"heartbeat_at": utc_now_iso()})
            atomic_write_text(self._path, refreshed.model_dump_json(indent=2), fsync=False)
            return refreshed

    def release(self, owner_id: str) -> bool:
        with self._guard:
            existing = self.read()
            if existing is None:
                return False
            if existing.owner_id != owner_id and not self.is_stale(existing):
                raise ProjectLockedError(self._project_id, existing)
            with contextlib.suppress(FileNotFoundError):
                self._path.unlink()
            return True

    def ensure_held(self, owner_id: str) -> None:
        """Raise ``PROJECT_LOCKED`` unless ``owner_id`` holds a live lock or no
        lock exists (a caller without a lock is allowed to edit when nobody
        else is editing; the lock protects against *concurrent* editors)."""
        existing = self.read()
        if existing is None:
            return
        if existing.owner_id == owner_id:
            return
        if self.is_stale(existing):
            return
        raise ProjectLockedError(self._project_id, existing)
