# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from afmlabel.domain.common import utc_now
from afmlabel.persistence.locks import ProjectLock, ProjectLockedError


def test_acquire_heartbeat_release(tmp_path: Path) -> None:
    lock = ProjectLock("proj_x", tmp_path / "project.lock")
    info = lock.acquire("tab-1", "alice")
    assert info.owner_id == "tab-1"
    assert lock.status("tab-1").owned_by_caller
    assert not lock.status("tab-2").owned_by_caller
    refreshed = lock.heartbeat("tab-1")
    assert refreshed.heartbeat_at >= info.heartbeat_at
    assert lock.release("tab-1")
    assert not lock.status("tab-1").locked


def test_live_lock_blocks_other_editor(tmp_path: Path) -> None:
    lock = ProjectLock("proj_x", tmp_path / "project.lock")
    lock.acquire("tab-1", "alice")
    with pytest.raises(ProjectLockedError) as excinfo:
        lock.acquire("tab-2", "bob")
    assert excinfo.value.code == "PROJECT_LOCKED"
    with pytest.raises(ProjectLockedError):
        lock.ensure_held("tab-2")
    lock.ensure_held("tab-1")
    # Forced takeover is explicit.
    info = lock.acquire("tab-2", "bob", force=True)
    assert info.owner_id == "tab-2"


def test_stale_lock_is_recoverable(tmp_path: Path) -> None:
    lock = ProjectLock("proj_x", tmp_path / "project.lock", stale_after_seconds=5)
    info = lock.acquire("tab-1", "alice")
    old = (utc_now() - timedelta(seconds=60)).isoformat(timespec="milliseconds")
    (tmp_path / "project.lock").write_text(
        info.model_copy(update={"heartbeat_at": old}).model_dump_json()
    )
    assert lock.status("tab-2").stale
    lock.ensure_held("tab-2")  # stale lock does not block
    taken = lock.acquire("tab-2", "bob")
    assert taken.owner_id == "tab-2"


def test_corrupt_lock_file_is_treated_as_stale(tmp_path: Path) -> None:
    path = tmp_path / "project.lock"
    path.write_text("{not json")
    lock = ProjectLock("proj_x", path)
    assert lock.read() is None
    assert lock.acquire("tab-1", "alice").owner_id == "tab-1"


def test_heartbeat_without_lock_raises(tmp_path: Path) -> None:
    lock = ProjectLock("proj_x", tmp_path / "project.lock")
    with pytest.raises(ProjectLockedError.__mro__[1]):
        lock.heartbeat("tab-1")
