# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Local model manifest written by ``scripts/download_models.py``.

The manifest is the only thing the runtime trusts about a checkpoint:
repository, pinned revision, per-file SHA-256 hashes and the official code
revision the checkpoint was validated against. It never contains tokens.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

MANIFEST_FILENAME = "manifest.json"
MANIFEST_SCHEMA_VERSION = 1


class ManifestFile(BaseModel):
    path: str
    size_bytes: int
    sha256: str


class ModelManifest(BaseModel):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    model_id: str
    repository: str
    revision: str
    downloaded_at: str
    files: list[ManifestFile] = Field(default_factory=list)
    #: SHA-256 of the primary checkpoint file (first ``.pt``/``.safetensors``).
    checkpoint_sha256: str | None = None
    checkpoint_file: str | None = None
    code_revision: str | None = None
    license_note: str | None = None


def sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    *,
    model_id: str,
    repository: str,
    revision: str,
    root: Path,
    code_revision: str | None = None,
    license_note: str | None = None,
) -> ModelManifest:
    files: list[ManifestFile] = []
    checkpoint_sha: str | None = None
    checkpoint_file: str | None = None
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative == MANIFEST_FILENAME or relative.startswith(".cache/"):
            continue
        digest = sha256_of_file(path)
        files.append(ManifestFile(path=relative, size_bytes=path.stat().st_size, sha256=digest))
        if checkpoint_sha is None and path.suffix in {".pt", ".pth", ".safetensors", ".bin"}:
            checkpoint_sha = digest
            checkpoint_file = relative
    return ModelManifest(
        model_id=model_id,
        repository=repository,
        revision=revision,
        downloaded_at=datetime.now(tz=UTC).isoformat(),
        files=files,
        checkpoint_sha256=checkpoint_sha,
        checkpoint_file=checkpoint_file,
        code_revision=code_revision,
        license_note=license_note,
    )


def write_manifest(manifest: ModelManifest, root: Path) -> Path:
    path = root / MANIFEST_FILENAME
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_manifest(path: Path) -> ModelManifest:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    return ModelManifest.model_validate(data)


def verify_manifest(manifest: ModelManifest, root: Path) -> list[str]:
    """Return a list of problems (empty when every file matches its hash)."""
    problems: list[str] = []
    for entry in manifest.files:
        path = root / entry.path
        if not path.exists():
            problems.append(f"missing file: {entry.path}")
            continue
        if path.stat().st_size != entry.size_bytes:
            problems.append(f"size mismatch: {entry.path}")
            continue
        if sha256_of_file(path) != entry.sha256:
            problems.append(f"sha256 mismatch: {entry.path}")
    if manifest.checkpoint_sha256 is None:
        problems.append("manifest does not identify a checkpoint file")
    return problems
