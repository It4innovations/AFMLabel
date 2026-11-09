# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import BaseModel

from afmlabel.persistence import atomic
from afmlabel.persistence.atomic import (
    atomic_write_json,
    load_json,
    load_model,
    remove_stale_temp_files,
    save_model,
)


class Doc(BaseModel):
    value: int


def test_atomic_write_replaces_destination(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"value": 1})
    atomic_write_json(path, {"value": 2})
    assert load_json(path) == {"value": 2}
    assert not list(tmp_path.glob(".*.tmp"))


def test_interrupted_write_leaves_previous_file_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"value": 1})

    def crash(*_: object, **__: object) -> None:
        raise OSError("simulated crash before replace")

    monkeypatch.setattr(atomic.os, "fsync", crash)
    with pytest.raises(OSError):
        atomic_write_json(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 1}
    assert not list(tmp_path.glob(".*.tmp")), "temporary file must be cleaned up"


def test_replace_failure_does_not_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"value": 1})

    def crash(*_: object, **__: object) -> None:
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(atomic.os, "replace", crash)
    with pytest.raises(OSError):
        atomic_write_json(path, {"value": 2})
    assert json.loads(path.read_text()) == {"value": 1}


def test_model_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "doc.json"
    save_model(path, Doc(value=7))
    assert load_model(path, Doc) == Doc(value=7)


def test_stale_temp_files_are_removed(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    stale = tmp_path / "sub" / ".state.json.abc.tmp"
    stale.write_text("partial")
    keep = tmp_path / "sub" / "state.json"
    keep.write_text("{}")
    removed = remove_stale_temp_files(tmp_path)
    assert removed == [stale]
    assert keep.exists()


def test_write_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c.json"
    atomic_write_json(path, {"ok": True})
    assert os.path.exists(path)


def test_replace_with_retry_survives_transient_permission_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from afmlabel.persistence import atomic

    source = tmp_path / ".staging"
    source.mkdir()
    (source / "f.txt").write_text("x", encoding="utf-8")
    destination = tmp_path / "final"
    real_replace = atomic.os.replace
    calls = {"n": 0}

    def flaky(src: object, dst: object) -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(atomic.os, "replace", flaky)
    atomic.replace_with_retry(source, destination, first_delay=0.001)
    assert destination.is_dir() and not source.exists() and calls["n"] == 3

    monkeypatch.setattr(
        atomic.os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(5, "x"))
    )
    with pytest.raises(PermissionError):
        atomic.replace_with_retry(destination, tmp_path / "other", attempts=2, first_delay=0.001)
