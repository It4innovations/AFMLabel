# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Dataset service: creation from upload batches, listing, mutation,
source replacement and display cache."""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from afmlabel.domain.common import new_id, utc_now_iso
from afmlabel.domain.dataset import (
    Dataset,
    DatasetSummary,
    SourceImage,
    SourceReplacement,
    SourceSummary,
    UnknownChannelError,
)
from afmlabel.domain.events import EventSource
from afmlabel.domain.project import ChannelKey, HistoryEntry
from afmlabel.errors import AppError, NotFoundError, RevisionConflictError
from afmlabel.images.decode import (
    DecodedImage,
    ImageDecodeError,
    calibrations_compatible,
    decode_image,
    sha256_of_file,
)
from afmlabel.images.display import render_display_cache
from afmlabel.imports.batches import BatchFileRecord, BatchStore, UploadBatch
from afmlabel.persistence import paths
from afmlabel.persistence.atomic import load_json, replace_with_retry, save_model
from afmlabel.persistence.migrations import migrate
from afmlabel.projects.store import ProjectStore

logger = logging.getLogger(__name__)


class DatasetNotFoundError(NotFoundError):
    def __init__(self, project_id: str, dataset_id: str) -> None:
        super().__init__(
            "DATASET_NOT_FOUND",
            f"dataset {dataset_id} does not exist in project {project_id}",
            target={"project_id": project_id, "dataset_id": dataset_id},
        )


class ChannelNotFoundError(NotFoundError):
    def __init__(self, dataset_id: str, channel: str) -> None:
        super().__init__(
            "CHANNEL_NOT_FOUND",
            f"dataset {dataset_id} has no channel {channel!r}",
            target={"dataset_id": dataset_id, "channel": channel},
            recovery="use one of the project's channel keys",
        )


class DatasetStore:
    def __init__(self, store: ProjectStore, batches: BatchStore, *, fsync: bool = True) -> None:
        self._projects = store
        self._batches = batches
        self._fsync = fsync
        self._guards: dict[str, threading.Lock] = {}
        self._guards_lock = threading.Lock()
        #: Hook that counts regions so summaries can show them.
        self.region_counter: Callable[[Path], int] = lambda _: 0

    # ---------------------------------------------------------------- paths

    def dataset_dir(self, project_id: str, dataset_id: str) -> Path:
        if (
            not dataset_id.startswith("ds_")
            or "/" in dataset_id
            or "\\" in dataset_id
            or ".." in dataset_id
        ):
            raise DatasetNotFoundError(project_id, dataset_id)
        return paths.dataset_dir(self._projects.project_dir(project_id), dataset_id)

    def _guard(self, key: str) -> threading.Lock:
        with self._guards_lock:
            guard = self._guards.get(key)
            if guard is None:
                guard = threading.Lock()
                self._guards[key] = guard
            return guard

    @staticmethod
    def require_source(dataset: Dataset, channel: ChannelKey) -> SourceImage:
        try:
            return dataset.source(channel)
        except UnknownChannelError:
            raise ChannelNotFoundError(dataset.dataset_id, channel) from None

    def source_path(self, dataset: Dataset, channel: ChannelKey) -> Path:
        ddir = self.dataset_dir(dataset.project_id, dataset.dataset_id)
        source = self.require_source(dataset, channel)
        return paths.dataset_source_dir(ddir) / source.stored_filename

    def display_path(self, project_id: str, dataset_id: str, channel: ChannelKey) -> Path:
        return (
            paths.dataset_cache_dir(self.dataset_dir(project_id, dataset_id))
            / "display"
            / f"{channel}.png"
        )

    def thumbnail_path(self, project_id: str, dataset_id: str, channel: ChannelKey) -> Path:
        return (
            paths.dataset_cache_dir(self.dataset_dir(project_id, dataset_id))
            / "thumbnails"
            / f"{channel}.png"
        )

    # ---------------------------------------------------------------- reads

    def get(self, project_id: str, dataset_id: str) -> Dataset:
        ddir = self.dataset_dir(project_id, dataset_id)
        path = paths.dataset_file(ddir)
        if not path.exists():
            raise DatasetNotFoundError(project_id, dataset_id)
        return Dataset.model_validate(migrate("dataset", load_json(path)))

    def summary(self, dataset: Dataset) -> DatasetSummary:
        ddir = self.dataset_dir(dataset.project_id, dataset.dataset_id)
        return DatasetSummary(
            dataset_id=dataset.dataset_id,
            name=dataset.name,
            pairing_key=dataset.pairing_key,
            width=dataset.width,
            height=dataset.height,
            revision=dataset.revision,
            status=dataset.status,
            block_reason=dataset.block_reason,
            validated_revision=dataset.validated_revision,
            region_count=self.region_counter(ddir),
            created_at=dataset.created_at,
            modified_at=dataset.modified_at,
            sources=[
                SourceSummary(
                    channel=s.channel, original_filename=s.original_filename, sha256=s.sha256
                )
                for s in dataset.channels
            ],
        )

    def list_datasets(self, project_id: str) -> list[DatasetSummary]:
        project = self._projects.get_project(project_id)
        summaries: list[DatasetSummary] = []
        for dataset_id in project.dataset_order:
            try:
                summaries.append(self.summary(self.get(project_id, dataset_id)))
            except (AppError, ValueError, OSError) as exc:
                logger.error(
                    "skipping unreadable dataset",
                    extra={"project_id": project_id, "dataset_id": dataset_id, "error": str(exc)},
                )
        return summaries

    def neighbours(self, project_id: str, dataset_id: str) -> tuple[str | None, str | None]:
        order = self._projects.get_project(project_id).dataset_order
        if dataset_id not in order:
            return None, None
        index = order.index(dataset_id)
        previous = order[index - 1] if index > 0 else None
        following = order[index + 1] if index + 1 < len(order) else None
        return previous, following

    def load_source_array(self, dataset: Dataset, channel: ChannelKey) -> DecodedImage:
        return decode_image(self.source_path(dataset, channel))

    def ensure_display_cache(self, dataset: Dataset, channel: ChannelKey) -> Path:
        display = self.display_path(dataset.project_id, dataset.dataset_id, channel)
        thumbnail = self.thumbnail_path(dataset.project_id, dataset.dataset_id, channel)
        source = self.source_path(dataset, channel)
        if (
            display.exists()
            and thumbnail.exists()
            and display.stat().st_mtime >= source.stat().st_mtime
        ):
            return display
        decoded = decode_image(source)
        render_display_cache(decoded.array, display, thumbnail)
        return display

    # -------------------------------------------------------------- creation

    def _source_image(
        self, record: BatchFileRecord, channel: ChannelKey, stored_filename: str
    ) -> SourceImage:
        if record.error or record.width is None or record.height is None:
            raise AppError(
                "PAIR_MISSING_IMAGE",
                f"{record.original_filename} did not decode: {record.error}",
                status_code=422,
                target={"file_id": record.file_id},
            )
        return SourceImage(
            channel=channel,
            stored_filename=stored_filename,
            original_filename=record.original_filename,
            sha256=record.sha256,
            size_bytes=record.size_bytes,
            width=record.width,
            height=record.height,
            dtype=record.dtype or "unknown",
            bit_depth=record.bit_depth or 0,
            channels=record.channels or 0,
            format=record.format or "unknown",
            lossy_format=record.lossy_format,
            calibration=record.calibration,
            imported_at=utc_now_iso(),
            metadata=record.metadata,
        )

    def commit_batch(
        self,
        project_id: str,
        batch: UploadBatch,
        pairs: list[tuple[str, dict[ChannelKey, str]]],
        *,
        pairing_method: str,
        expected_revision: int,
        actor: str | None,
    ) -> list[Dataset]:
        """Create one dataset per ``(pairing_key, {channel_key: file_id})``.

        Every project channel must be present exactly once. All-or-nothing:
        datasets are staged under ``datasets/.staging-<id>`` and only after
        every pair validated are they renamed into place and the project
        updated. Any failure removes the staged directories.
        """
        project = self._projects.get_project(project_id)
        channel_keys = project.channel_keys
        labels = {c.key: c.label for c in project.channels}
        pdir = self._projects.project_dir(project_id)
        by_id = {f.file_id: f for f in batch.files}
        staged: list[tuple[Path, Path, Dataset]] = []
        try:
            for pairing_key, selection in pairs:
                if sorted(selection) != sorted(channel_keys):
                    raise AppError(
                        "PAIR_MISSING_IMAGE",
                        f"pair {pairing_key}: a file is required for every channel "
                        f"({', '.join(labels[k] for k in channel_keys)})",
                        status_code=422,
                        target={"pairing_key": pairing_key},
                    )
                sources: list[SourceImage] = []
                records: list[BatchFileRecord] = []
                for key in channel_keys:
                    record = by_id.get(selection[key])
                    if record is None:
                        raise AppError(
                            "PAIR_MISSING_IMAGE",
                            f"pair {pairing_key}: unknown file id",
                            status_code=422,
                            target={"pairing_key": pairing_key},
                        )
                    if record.channel != key:
                        raise AppError(
                            "PAIR_MISSING_IMAGE",
                            f"pair {pairing_key}: {record.original_filename} was uploaded for "
                            f"channel {record.channel!r}, not {key!r}",
                            status_code=422,
                            target={"pairing_key": pairing_key},
                        )
                    ext = Path(record.stored_filename).suffix
                    sources.append(self._source_image(record, key, f"{key}{ext}"))
                    records.append(record)
                first = sources[0]
                for source in sources[1:]:
                    if (first.width, first.height) != (source.width, source.height):
                        raise AppError(
                            "PAIR_DIMENSION_MISMATCH",
                            f"pair {pairing_key}: {labels[first.channel]} "
                            f"{first.width}x{first.height} vs {labels[source.channel]} "
                            f"{source.width}x{source.height}",
                            status_code=422,
                            target={"pairing_key": pairing_key},
                            recovery="re-export all channels with identical dimensions",
                        )
                    if not calibrations_compatible(first.calibration, source.calibration):
                        raise AppError(
                            "PAIR_REGISTRATION_MISMATCH",
                            f"pair {pairing_key}: calibration differs between "
                            f"{labels[first.channel]} and {labels[source.channel]}",
                            status_code=422,
                            target={"pairing_key": pairing_key},
                            recovery="check the AFM export; automatic registration is "
                            "not supported",
                        )
                dataset_id = new_id("ds")
                now = utc_now_iso()
                dataset = Dataset(
                    dataset_id=dataset_id,
                    project_id=project_id,
                    name=pairing_key,
                    pairing_key=pairing_key,
                    channels=sources,
                    width=first.width,
                    height=first.height,
                    created_at=now,
                    modified_at=now,
                    import_batch_id=batch.batch_id,
                    pairing_method=pairing_method,  # type: ignore[arg-type]
                )
                staging = paths.datasets_dir(pdir) / f".staging-{dataset_id}"
                final = paths.dataset_dir(pdir, dataset_id)
                source_dir = paths.dataset_source_dir(staging)
                source_dir.mkdir(parents=True, exist_ok=False)
                for sub in (
                    paths.dataset_annotations_dir(staging) / "masks",
                    paths.dataset_imports_dir(staging),
                    paths.dataset_cache_dir(staging) / "display",
                    paths.dataset_cache_dir(staging) / "thumbnails",
                    paths.dataset_jobs_dir(staging),
                    paths.dataset_validation_dir(staging),
                ):
                    sub.mkdir(parents=True, exist_ok=True)
                for record, source in zip(records, sources, strict=True):
                    shutil.copy2(
                        self._batches.file_path(batch, record), source_dir / source.stored_filename
                    )
                    if sha256_of_file(source_dir / source.stored_filename) != source.sha256:
                        raise AppError(
                            "UPLOAD_INVALID",
                            f"pair {pairing_key}: {labels[source.channel]} checksum changed "
                            "during import",
                            status_code=500,
                            target={"pairing_key": pairing_key},
                        )
                save_model(paths.dataset_file(staging), dataset, fsync=self._fsync)
                staged.append((staging, final, dataset))
        except BaseException:
            for staging, _final, _ in staged:
                shutil.rmtree(staging, ignore_errors=True)
            raise

        # Every pair validated: publish directories, then update the project.
        published: list[Dataset] = []
        for staging, final, dataset in staged:
            replace_with_retry(staging, final)
            published.append(dataset)

        def mutate(project: Any, _: Any) -> None:
            project.dataset_order.extend(d.dataset_id for d in published)
            project.import_history.append(
                HistoryEntry(
                    kind="import_pairs",
                    entry_id=batch.batch_id,
                    at=utc_now_iso(),
                    summary=f"imported {len(published)} dataset(s) ({pairing_method} pairing, "
                    f"{len(channel_keys)} channel(s))",
                    details={"dataset_ids": [d.dataset_id for d in published]},
                )
            )

        self._projects.mutate_project(
            project_id,
            expected_revision,
            mutate,
            operation="datasets.import",
            actor=actor,
            source="pair_import",
            details={
                "batch_id": batch.batch_id,
                "dataset_ids": [d.dataset_id for d in published],
                "pairing_method": pairing_method,
            },
        )
        for dataset in published:
            self._projects.record_event(
                project_id,
                operation="dataset.create",
                dataset_id=dataset.dataset_id,
                new_revision=dataset.revision,
                source="pair_import",
                actor=actor,
                provenance={
                    "source_sha256": {s.channel: s.sha256 for s in dataset.channels},
                    "batch_id": batch.batch_id,
                },
            )
        return published

    # ------------------------------------------------------------- mutation

    def mutate(
        self,
        project_id: str,
        dataset_id: str,
        expected_revision: int | None,
        mutate: Callable[[Dataset], None],
        *,
        operation: str,
        actor: str | None,
        source: EventSource = "manual",
        invalidates_validation: bool = True,
        details: dict[str, Any] | None = None,
        provenance: dict[str, Any] | None = None,
        region_ids: list[str] | None = None,
        class_ids: list[int] | None = None,
    ) -> Dataset:
        ddir = self.dataset_dir(project_id, dataset_id)
        with self._guard(f"{project_id}/{dataset_id}"):
            dataset = self.get(project_id, dataset_id)
            if expected_revision is not None and dataset.revision != expected_revision:
                raise RevisionConflictError(
                    {"project_id": project_id, "dataset_id": dataset_id},
                    expected_revision,
                    dataset.revision,
                )
            previous = dataset.revision
            mutate(dataset)
            dataset.revision = previous + 1
            dataset.modified_at = utc_now_iso()
            if invalidates_validation:
                dataset.validated_revision = None
            save_model(paths.dataset_file(ddir), dataset, fsync=self._fsync)
        self._projects.record_event(
            project_id,
            operation=operation,
            dataset_id=dataset_id,
            previous_revision=previous,
            new_revision=dataset.revision,
            source=source,
            actor=actor,
            details=details or {},
            provenance=provenance or {},
            region_ids=region_ids or [],
            class_ids=class_ids or [],
        )
        return dataset

    def set_validated_revision(
        self, project_id: str, dataset_id: str, validated_revision: int | None
    ) -> Dataset:
        """Record the validation input revision without bumping the dataset
        revision (validation is derived state, not an edit)."""
        ddir = self.dataset_dir(project_id, dataset_id)
        with self._guard(f"{project_id}/{dataset_id}"):
            dataset = self.get(project_id, dataset_id)
            if validated_revision is not None and validated_revision != dataset.revision:
                validated_revision = None
            dataset.validated_revision = validated_revision
            save_model(paths.dataset_file(ddir), dataset, fsync=self._fsync)
        return dataset

    def delete(
        self, project_id: str, dataset_id: str, expected_revision: int | None, *, actor: str | None
    ) -> Path:
        ddir = self.dataset_dir(project_id, dataset_id)
        with self._guard(f"{project_id}/{dataset_id}"):
            dataset = self.get(project_id, dataset_id)
            if expected_revision is not None and dataset.revision != expected_revision:
                raise RevisionConflictError(
                    {"project_id": project_id, "dataset_id": dataset_id},
                    expected_revision,
                    dataset.revision,
                )
            trash = paths.trash_dir(self._projects.workspace_root)
            trash.mkdir(parents=True, exist_ok=True)
            stamp = utc_now_iso().replace(":", "").replace("+", "_")
            destination = trash / f"{project_id}-{dataset_id}-{stamp}"
            shutil.move(str(ddir), str(destination))

        def mutate(project: Any, _: Any) -> None:
            project.dataset_order = [d for d in project.dataset_order if d != dataset_id]

        self._projects.mutate_project(
            self._projects.get_project(project_id).project_id,
            self._projects.get_project(project_id).revision,
            mutate,
            operation="dataset.delete",
            actor=actor,
            details={"dataset_id": dataset_id, "name": dataset.name},
        )
        return destination

    # ------------------------------------------------------ source replacement

    def replacement_report(
        self, dataset: Dataset, channel: ChannelKey, candidate: Path, original_filename: str
    ) -> dict[str, Any]:
        """Compatibility report for replacing ``channel`` with ``candidate``."""
        try:
            decoded = decode_image(candidate, display_name=original_filename)
        except ImageDecodeError as exc:
            return {"compatible": False, "code": "IMAGE_DECODE_FAILED", "reason": exc.message}
        current = self.require_source(dataset, channel)
        problems: list[str] = []
        code = None
        if (decoded.width, decoded.height) != (dataset.width, dataset.height):
            problems.append(
                f"dimensions {decoded.width}x{decoded.height} differ from dataset "
                f"{dataset.width}x{dataset.height}; existing annotations cannot be resampled"
            )
            code = "SOURCE_REPLACEMENT_INCOMPATIBLE"
        for other in dataset.channels:
            if other.channel == channel:
                continue
            if not calibrations_compatible(decoded.calibration, other.calibration):
                problems.append(f"calibration differs from channel {other.channel!r}")
                code = "PAIR_REGISTRATION_MISMATCH"
                break
        sha = sha256_of_file(candidate)
        warnings: list[str] = []
        if decoded.lossy_format:
            warnings.append("replacement is a lossy JPEG")
        if decoded.dtype != current.dtype or decoded.channels != current.channels:
            warnings.append(
                f"pixel type changes from {current.dtype}x{current.channels} to "
                f"{decoded.dtype}x{decoded.channels}"
            )
        if sha == current.sha256:
            warnings.append("replacement is byte-identical to the current source")
        return {
            "compatible": not problems,
            "code": code,
            "reason": "; ".join(problems) if problems else None,
            "warnings": warnings,
            "current": current.model_dump(),
            "candidate": {
                "original_filename": original_filename,
                "sha256": sha,
                "width": decoded.width,
                "height": decoded.height,
                "dtype": decoded.dtype,
                "channels": decoded.channels,
                "format": decoded.format,
                "calibration": decoded.calibration.model_dump(),
            },
        }

    def apply_replacement(
        self,
        project_id: str,
        dataset_id: str,
        channel: ChannelKey,
        candidate: Path,
        original_filename: str,
        *,
        expected_revision: int,
        actor: str | None,
    ) -> Dataset:
        dataset = self.get(project_id, dataset_id)
        report = self.replacement_report(dataset, channel, candidate, original_filename)
        if not report["compatible"]:
            raise AppError(
                report["code"] or "SOURCE_REPLACEMENT_INCOMPATIBLE",
                report["reason"],
                status_code=422,
                target={"project_id": project_id, "dataset_id": dataset_id, "channel": channel},
                recovery="export a replacement with identical dimensions and calibration",
            )
        decoded = decode_image(candidate, display_name=original_filename)
        ddir = self.dataset_dir(project_id, dataset_id)
        source_dir = paths.dataset_source_dir(ddir)
        stored = f"{channel}{Path(original_filename).suffix.lower() or candidate.suffix.lower()}"
        new_source = SourceImage(
            channel=channel,
            stored_filename=stored,
            original_filename=original_filename,
            sha256=report["candidate"]["sha256"],
            size_bytes=candidate.stat().st_size,
            width=decoded.width,
            height=decoded.height,
            dtype=decoded.dtype,
            bit_depth=decoded.bit_depth,
            channels=decoded.channels,
            format=decoded.format,
            lossy_format=decoded.lossy_format,
            calibration=decoded.calibration,
            imported_at=utc_now_iso(),
            metadata=dict(decoded.metadata),
        )
        del decoded

        def mutate(current: Dataset) -> None:
            previous = self.require_source(current, channel)
            # Copy alongside, then atomically swap the reference in dataset.json.
            temp = source_dir / f".{stored}.incoming"
            shutil.copy2(candidate, temp)
            final = source_dir / stored
            if previous.stored_filename != stored:
                (source_dir / previous.stored_filename).unlink(missing_ok=True)
            temp.replace(final)
            current.replacements.append(
                SourceReplacement(
                    channel=channel,
                    replaced_at=utc_now_iso(),
                    previous_sha256=previous.sha256,
                    new_sha256=new_source.sha256,
                    previous_original_filename=previous.original_filename,
                    new_original_filename=original_filename,
                    dataset_revision_before=current.revision,
                )
            )
            current.replace_source(new_source)

        updated = self.mutate(
            project_id,
            dataset_id,
            expected_revision,
            mutate,
            operation=f"dataset.replace_source.{channel}",
            actor=actor,
            details={"original_filename": original_filename, "warnings": report["warnings"]},
            provenance={
                "channel": channel,
                "previous_sha256": self.require_source(dataset, channel).sha256,
                "new_sha256": new_source.sha256,
            },
        )
        for path in (
            self.display_path(project_id, dataset_id, channel),
            self.thumbnail_path(project_id, dataset_id, channel),
        ):
            path.unlink(missing_ok=True)
        return updated
