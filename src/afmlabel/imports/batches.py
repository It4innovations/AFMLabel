# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Upload batches for paired-image import.

A batch is a temporary directory under ``workspace/tmp/uploads/<batch_id>``
holding validated files plus ``batch.json``. Files are streamed to
``<name>.part`` and renamed once complete, so an interrupted upload never
looks like a finished one. Committing a batch creates datasets atomically;
discarding or expiring it removes the directory.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, Field

from afmlabel.domain.common import new_id, utc_now_iso
from afmlabel.domain.dataset import Calibration
from afmlabel.domain.project import ChannelDefinition, ChannelKey
from afmlabel.errors import AppError, NotFoundError
from afmlabel.images.decode import (
    SUPPORTED_EXTENSIONS,
    ImageDecodeError,
    decode_image,
    sha256_of_file,
)
from afmlabel.imports.pairing import (
    BatchFile,
    PairingPreview,
    build_preview,
    channel_tokens,
    extract_pairing_key,
)
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_model, save_model
from afmlabel.projects.sanitize import safe_filename

logger = logging.getLogger(__name__)

BATCH_FILE = "batch.json"
BATCH_MAX_AGE_SECONDS = 24 * 3600


class BatchFileRecord(BaseModel):
    file_id: str
    #: Project channel key the file was uploaded for.
    channel: ChannelKey
    original_filename: str
    stored_filename: str
    pairing_key: str
    sha256: str
    size_bytes: int
    width: int | None = None
    height: int | None = None
    dtype: str | None = None
    bit_depth: int | None = None
    channels: int | None = None
    format: str | None = None
    lossy_format: bool = False
    calibration: Calibration = Field(default_factory=Calibration)
    metadata: dict[str, object] = Field(default_factory=dict)
    error: str | None = None

    def calibration_signature(self) -> str | None:
        c = self.calibration
        if c.pixel_size_x is None:
            return None
        return f"{c.pixel_size_x:.9g}x{c.pixel_size_y:.9g}{c.unit or ''}"

    def to_batch_file(self) -> BatchFile:
        return BatchFile(
            file_id=self.file_id,
            channel=self.channel,
            original_filename=self.original_filename,
            pairing_key=self.pairing_key,
            width=self.width,
            height=self.height,
            error=self.error,
            calibration_signature=self.calibration_signature(),
        )


class UploadBatch(BaseModel):
    batch_id: str
    project_id: str
    created_at: str
    files: list[BatchFileRecord] = Field(default_factory=list)


class PairRowResponse(BaseModel):
    pairing_key: str
    status: str
    message: str
    #: One entry per project channel key (``None`` when the slot is empty).
    files: dict[ChannelKey, BatchFileRecord | None] = Field(default_factory=dict)
    missing_channels: list[ChannelKey] = Field(default_factory=list)
    candidates: list[BatchFileRecord] = Field(default_factory=list)


class PairingPreviewResponse(BaseModel):
    batch_id: str
    method: str
    ok_count: int
    problem_count: int
    order_based_fallback_available: bool
    channel_keys: list[ChannelKey] = Field(default_factory=list)
    rows: list[PairRowResponse]
    files: list[BatchFileRecord]


class BatchNotFoundError(NotFoundError):
    def __init__(self, batch_id: str) -> None:
        super().__init__(
            "UPLOAD_INVALID",
            f"upload batch {batch_id} does not exist or has expired",
            target={"batch_id": batch_id},
            recovery="start the import again",
        )


class BatchStore:
    def __init__(self, workspace_root: Path, *, max_file_size_bytes: int, chunk_size: int) -> None:
        self._root = paths.uploads_tmp_dir(workspace_root)
        self._max_size = max_file_size_bytes
        self._chunk = chunk_size
        self._root.mkdir(parents=True, exist_ok=True)

    def batch_dir(self, batch_id: str) -> Path:
        if (
            not batch_id.startswith("batch_")
            or "/" in batch_id
            or "\\" in batch_id
            or ".." in batch_id
        ):
            raise BatchNotFoundError(batch_id)
        return self._root / batch_id

    def create(self, project_id: str) -> UploadBatch:
        batch = UploadBatch(
            batch_id=new_id("batch"), project_id=project_id, created_at=utc_now_iso()
        )
        directory = self.batch_dir(batch.batch_id)
        directory.mkdir(parents=True, exist_ok=False)
        self._save(batch)
        return batch

    def _save(self, batch: UploadBatch) -> None:
        save_model(self.batch_dir(batch.batch_id) / BATCH_FILE, batch, fsync=False)

    def get(self, batch_id: str, project_id: str) -> UploadBatch:
        path = self.batch_dir(batch_id) / BATCH_FILE
        if not path.exists():
            raise BatchNotFoundError(batch_id)
        batch = load_model(path, UploadBatch)
        if batch.project_id != project_id:
            raise BatchNotFoundError(batch_id)
        return batch

    def discard(self, batch_id: str) -> None:
        directory = self.batch_dir(batch_id)
        if directory.exists():
            shutil.rmtree(directory, ignore_errors=True)

    def file_path(self, batch: UploadBatch, record: BatchFileRecord) -> Path:
        return self.batch_dir(batch.batch_id) / record.stored_filename

    def add_file(
        self,
        batch: UploadBatch,
        channel: ChannelKey,
        original_filename: str,
        stream: BinaryIO,
        *,
        channels: list[ChannelDefinition] | None = None,
    ) -> BatchFileRecord:
        safe = safe_filename(original_filename, default="image")
        suffix = Path(safe).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise AppError(
                "UPLOAD_INVALID",
                f"{original_filename}: unsupported file type {suffix or '(none)'}",
                status_code=422,
                target={"filename": original_filename},
                recovery="upload TIFF or PNG files",
            )
        file_id = new_id("file", 8)
        stored = f"{file_id}{suffix}"
        directory = self.batch_dir(batch.batch_id)
        final_path = directory / stored
        part_path = directory / f"{stored}.part"
        size = 0
        with part_path.open("wb") as handle:
            while True:
                chunk = stream.read(self._chunk)
                if not chunk:
                    break
                size += len(chunk)
                if size > self._max_size:
                    handle.close()
                    part_path.unlink(missing_ok=True)
                    raise AppError(
                        "UPLOAD_TOO_LARGE",
                        f"{original_filename} exceeds the maximum upload size "
                        f"({self._max_size // (1024 * 1024)} MB)",
                        status_code=413,
                        target={"filename": original_filename},
                        recovery="raise upload.maximum_file_size_mb or split the batch",
                    )
                handle.write(chunk)
        part_path.replace(final_path)

        tokens = channel_tokens(channels) if channels is not None else None
        record = BatchFileRecord(
            file_id=file_id,
            channel=channel,
            original_filename=original_filename,
            stored_filename=stored,
            pairing_key=extract_pairing_key(original_filename, channel, tokens=tokens),
            sha256=sha256_of_file(final_path),
            size_bytes=size,
        )
        try:
            decoded = decode_image(final_path, display_name=original_filename)
        except ImageDecodeError as exc:
            record.error = exc.message
        else:
            record.width, record.height = decoded.width, decoded.height
            record.dtype, record.bit_depth = decoded.dtype, decoded.bit_depth
            record.channels, record.format = decoded.channels, decoded.format
            record.lossy_format = decoded.lossy_format
            record.calibration = decoded.calibration
            record.metadata = dict(decoded.metadata)
            del decoded
        batch.files.append(record)
        self._save(batch)
        return record

    def remove_file(self, batch: UploadBatch, file_id: str) -> None:
        remaining = [f for f in batch.files if f.file_id != file_id]
        if len(remaining) == len(batch.files):
            raise NotFoundError(
                "UPLOAD_INVALID",
                f"file {file_id} is not part of this batch",
                target={"file_id": file_id},
            )
        for record in batch.files:
            if record.file_id == file_id:
                self.file_path(batch, record).unlink(missing_ok=True)
        batch.files = remaining
        self._save(batch)

    def preview(
        self,
        batch: UploadBatch,
        *,
        channels: list[ChannelDefinition] | None = None,
        allow_order_based: bool = False,
    ) -> PairingPreviewResponse:
        preview = self.raw_preview(batch, channels=channels, allow_order_based=allow_order_based)
        by_id = {f.file_id: f for f in batch.files}
        return PairingPreviewResponse(
            batch_id=batch.batch_id,
            method=preview.method,
            ok_count=preview.ok_count,
            problem_count=preview.problem_count,
            order_based_fallback_available=preview.order_based_fallback_available,
            channel_keys=preview.channel_keys,
            rows=[
                PairRowResponse(
                    pairing_key=row.pairing_key,
                    status=row.status,
                    message=row.message,
                    files={
                        key: (by_id[item.file_id] if item is not None else None)
                        for key, item in row.files.items()
                    },
                    missing_channels=row.missing_channels,
                    candidates=[by_id[c.file_id] for c in row.candidates],
                )
                for row in preview.rows
            ],
            files=list(batch.files),
        )

    def raw_preview(
        self,
        batch: UploadBatch,
        *,
        channels: list[ChannelDefinition] | None = None,
        allow_order_based: bool = False,
    ) -> PairingPreview:
        return build_preview(
            [f.to_batch_file() for f in batch.files],
            channels=channels,
            allow_order_based=allow_order_based,
        )

    def iter_batches(self) -> Iterator[UploadBatch]:
        for directory in self._root.iterdir():
            path = directory / BATCH_FILE
            if path.exists():
                try:
                    yield load_model(path, UploadBatch)
                except (OSError, ValueError):
                    continue

    def cleanup_expired(self, max_age_seconds: float = BATCH_MAX_AGE_SECONDS) -> int:
        removed = 0
        now = time.time()
        for directory in list(self._root.iterdir()):
            try:
                age = now - directory.stat().st_mtime
            except OSError:
                continue
            if age > max_age_seconds:
                shutil.rmtree(directory, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("removed expired upload batches", extra={"count": removed})
        return removed


__all__ = [
    "BatchFileRecord",
    "BatchNotFoundError",
    "BatchStore",
    "PairingPreviewResponse",
    "UploadBatch",
    "asdict",
]
