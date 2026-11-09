# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Meta SAM 2.1 image backend through the Hugging Face ``transformers`` port.

Why a third backend: the pinned ``facebook/sam3.1`` checkpoint is
CUDA-only and offers no mask prompt, and ``facebook/sam3`` is a 3.4 GB gated
download that needs a GPU in practice. SAM 2.1 base-plus (``facebook/
sam2.1-hiera-base-plus``, Apache-2.0, not gated, about 320 MB) runs on a CPU
within the 32 GB deployment profile and supports point, box and mask
prompts plus the classic three multi-mask proposals. It has no text
prompt; that stays a SAM 3 / 3.1 feature and is never emulated.

The port is used as an *image* model (``Sam2Model`` / ``Sam2Processor``):
the image embedding is computed once per distinct image (hash of the 8-bit
RGB render) and reused for every prompt on it. Nothing runs at import time;
``torch``/``transformers`` are imported in :meth:`Sam21Backend.load`. The
model is loaded with ``local_files_only=True`` from the pinned snapshot and
the loading report is inspected so a partially initialised model is refused.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

from afmlabel.images.display import to_rgb_uint8
from afmlabel.segmentation.base import (
    BackendCapability,
    BackendCapabilityError,
    BackendDescriptor,
    BackendOutOfMemoryError,
    BackendUnavailableError,
    PointPrompt,
    SegmentationProposal,
    image_height_width,
    validate_prompts_against_capabilities,
)
from afmlabel.segmentation.manifest import (
    MANIFEST_FILENAME,
    ModelManifest,
    load_manifest,
    verify_manifest,
)
from afmlabel.settings import RuntimeSettings, Sam21ModelSettings

logger = logging.getLogger(__name__)

BACKEND_ID = "sam21"
DISPLAY_NAME = "Meta SAM 2.1 base-plus (local, CPU capable)"
_DIAG_SIZE = 96
_MASK_LOGIT = 10.0


def _import(name: str) -> Any:
    return importlib.import_module(name)


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, MemoryError)
        or type(exc).__name__ in ("OutOfMemoryError", "CudaOutOfMemoryError")
        or "out of memory" in text
    )


class Sam21Backend:
    backend_id = BACKEND_ID

    def __init__(self, settings: Sam21ModelSettings, runtime: RuntimeSettings) -> None:
        self._settings = settings
        self._runtime = runtime
        self._manifest: ModelManifest | None = None
        self._loaded = False
        self._fatal_reason: str | None = None
        self._load_error: str | None = None
        self._device: str | None = None
        self._dtype: str | None = None
        self._model: Any = None
        self._processor: Any = None
        self._verified: set[BackendCapability] = set()
        self._capability_errors: dict[str, str] = {}
        self._lock = threading.RLock()
        self._embedding_key: str | None = None
        self._embeddings: Any = None
        self._weight_report: dict[str, Any] = {}

    # ----------------------------------------------------------- availability

    def _manifest_path(self) -> Path:
        return self._settings.checkpoint_dir / MANIFEST_FILENAME

    def unavailable_reason(self) -> str | None:
        if self._fatal_reason is not None:
            return self._fatal_reason
        if not self._settings.enabled:
            return "disabled in configuration (models.sam21.enabled is false)"
        if importlib.util.find_spec("torch") is None:
            return "PyTorch is not installed; install the optional 'sam' extra"
        if importlib.util.find_spec("transformers") is None:
            return "transformers is not installed; install the optional 'sam' extra"
        manifest_path = self._manifest_path()
        if not manifest_path.exists():
            return (
                f"model manifest not found at {manifest_path}; run "
                "python scripts/download_models.py --model sam21"
            )
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            return f"model manifest invalid: {exc}"
        self._manifest = manifest
        if self._settings.revision and manifest.revision != self._settings.revision:
            return (
                f"manifest revision {manifest.revision} does not match the configured "
                f"models.sam21.revision {self._settings.revision}"
            )
        if manifest.repository != self._settings.repository:
            return (
                f"manifest repository {manifest.repository} does not match configured "
                f"{self._settings.repository}"
            )
        if self._load_error is not None:
            return self._load_error
        return None

    def _select_device(self) -> tuple[str | None, str | None]:
        torch = _import("torch")
        requested = (self._runtime.device or "auto").lower()
        if requested == "rocm":
            return None, "ROCm is not validated for the SAM 2.1 stack"
        if requested == "cpu":
            if not self._settings.allow_cpu:
                return None, "runtime.device is cpu but models.sam21.allow_cpu is false"
            return "cpu", None
        if requested not in ("auto", "cuda") and not requested.startswith("cuda:"):
            return None, f"unsupported runtime.device {requested!r}"
        if torch.cuda.is_available():
            if requested.startswith("cuda:"):
                index = int(requested.split(":")[1])
                if index >= torch.cuda.device_count():
                    return None, f"runtime.device {requested!r} does not exist"
                return f"cuda:{index}", None
            return f"cuda:{torch.cuda.current_device()}", None
        if requested == "auto" and self._settings.allow_cpu:
            return "cpu", None
        if requested == "auto":
            return None, "no CUDA device is available and models.sam21.allow_cpu is false"
        return None, f"runtime.device {requested!r} requested but no CUDA device is available"

    # ------------------------------------------------------------------ load

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            reason = self.unavailable_reason()
            if reason is not None:
                raise BackendUnavailableError(self.backend_id, reason)
            manifest = self._manifest
            assert manifest is not None
            root = self._settings.checkpoint_dir
            problems = verify_manifest(manifest, root)
            if problems:
                self._load_error = f"MODEL_CHECKPOINT_INVALID: {'; '.join(problems[:3])}"
                raise BackendUnavailableError(self.backend_id, self._load_error)
            device, device_reason = self._select_device()
            if device is None:
                self._load_error = device_reason
                raise BackendUnavailableError(self.backend_id, device_reason or "no device")
            try:
                torch = _import("torch")
                transformers = _import("transformers")
                logger.info("loading SAM 2.1", extra={"checkpoint_dir": str(root)})
                model, info = transformers.Sam2Model.from_pretrained(
                    str(root),
                    local_files_only=True,
                    dtype=torch.float32,
                    output_loading_info=True,
                )
                self._check_loading_info(info)
                model.to(device)
                model.eval()
                self._processor = transformers.Sam2Processor.from_pretrained(
                    str(root), local_files_only=True
                )
                self._model = model
                self._device = device
                self._dtype = "float32"
            except BackendUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.release()
                if _is_oom(exc):
                    self._load_error = f"MODEL_OOM while loading: {exc}"
                else:
                    self._load_error = f"model load failed: {type(exc).__name__}: {exc}"
                raise BackendUnavailableError(self.backend_id, self._load_error) from exc
            self._self_test()
            if not self._verified & {
                BackendCapability.POINT_PROMPTS,
                BackendCapability.BOX_PROMPTS,
            }:
                errors = "; ".join(f"{k}: {v}" for k, v in self._capability_errors.items())
                self.release()
                self._load_error = f"diagnostic inference failed: {errors}"
                raise BackendUnavailableError(self.backend_id, self._load_error)
            self._loaded = True
            logger.info(
                "SAM 2.1 loaded",
                extra={
                    "device": self._device,
                    "capabilities": sorted(c.value for c in self._verified),
                    "weights": self._weight_report,
                },
            )

    def _check_loading_info(self, info: dict[str, Any]) -> None:
        missing = sorted(info.get("missing_keys", []) or [])
        unexpected = sorted(info.get("unexpected_keys", []) or [])
        mismatched = list(info.get("mismatched_keys", []) or [])
        self._weight_report = {
            "missing_count": len(missing),
            "unexpected_count": len(unexpected),
            "mismatched_count": len(mismatched),
            "missing": missing[:20],
        }
        if missing or mismatched:
            raise RuntimeError(
                f"{len(missing)} missing and {len(mismatched)} mismatched parameter(s) in the "
                f"checkpoint (e.g. {missing[:3] or mismatched[:3]}); refusing to run with "
                "random weights"
            )

    def _self_test(self) -> None:
        yy, xx = np.mgrid[:_DIAG_SIZE, :_DIAG_SIZE].astype(np.float64)
        blob = np.exp(-(((xx - 48) ** 2 + (yy - 48) ** 2) / (2 * 12.0**2)))
        image = to_rgb_uint8((blob * 60000).astype(np.uint16))
        self._verified = set()
        self._capability_errors = {}
        seed_mask: np.ndarray | None = None
        checks: list[tuple[BackendCapability, dict[str, Any]]] = [
            (
                BackendCapability.POINT_PROMPTS,
                {
                    "points": [PointPrompt(48, 48, True)],
                    "box_xyxy": None,
                    "previous_mask": None,
                    "text_prompt": None,
                },
            ),
            (
                BackendCapability.BOX_PROMPTS,
                {
                    "points": [],
                    "box_xyxy": (30.0, 30.0, 66.0, 66.0),
                    "previous_mask": None,
                    "text_prompt": None,
                },
            ),
        ]
        for capability, kwargs in checks:
            try:
                proposals = self._segment_unchecked(image, **kwargs)
                self._assert_proposals(proposals)
                if capability == BackendCapability.POINT_PROMPTS:
                    seed_mask = proposals[0].mask
                    if len(proposals) > 1:
                        self._verified.add(BackendCapability.MULTI_MASK_PROPOSALS)
                    else:
                        self._capability_errors[BackendCapability.MULTI_MASK_PROPOSALS.value] = (
                            "a single positive point returned one proposal only"
                        )
                self._verified.add(capability)
            except Exception as exc:  # noqa: BLE001
                self._capability_errors[capability.value] = f"{type(exc).__name__}: {exc}"
        if seed_mask is None:
            self._capability_errors[BackendCapability.MASK_PROMPTS.value] = (
                "no seed mask available (point prompt failed)"
            )
        else:
            try:
                proposals = self._segment_unchecked(
                    image,
                    points=[PointPrompt(48, 48, True), PointPrompt(8, 8, False)],
                    box_xyxy=None,
                    previous_mask=seed_mask,
                    text_prompt=None,
                )
                self._assert_proposals(proposals)
                proposals = self._segment_unchecked(
                    image, points=[], box_xyxy=None, previous_mask=seed_mask, text_prompt=None
                )
                self._assert_proposals(proposals)
                self._verified.add(BackendCapability.MASK_PROMPTS)
            except Exception as exc:  # noqa: BLE001
                self._capability_errors[BackendCapability.MASK_PROMPTS.value] = (
                    f"{type(exc).__name__}: {exc}"
                )
        self._capability_errors[BackendCapability.TEXT_PROMPTS.value] = (
            "SAM 2.1 has no text encoder; use sam3 or sam31 for text prompts"
        )
        self._drop_embeddings()

    @staticmethod
    def _assert_proposals(proposals: list[SegmentationProposal]) -> None:
        if not proposals:
            raise RuntimeError("no proposal returned")
        for proposal in proposals:
            if proposal.mask.shape != (_DIAG_SIZE, _DIAG_SIZE):
                raise RuntimeError(f"mask shape {proposal.mask.shape}")
            if proposal.mask.dtype != np.bool_:
                raise RuntimeError(f"mask dtype {proposal.mask.dtype}")

    # ------------------------------------------------------------ descriptor

    def descriptor(self) -> BackendDescriptor:
        reason = self.unavailable_reason()
        manifest = self._manifest
        warnings = [f"{c} not advertised: {e}" for c, e in self._capability_errors.items()]
        return BackendDescriptor(
            backend_id=self.backend_id,
            display_name=DISPLAY_NAME,
            capabilities=(
                frozenset(self._verified) if reason is None and self._loaded else frozenset()
            ),
            available=reason is None and self._loaded,
            unavailable_reason=(
                reason
                if reason is not None
                else (None if self._loaded else "model not loaded yet; loads on first use")
            ),
            model_repository=self._settings.repository,
            model_revision=manifest.revision if manifest else self._settings.revision,
            checkpoint_sha256=manifest.checkpoint_sha256 if manifest else None,
            code_revision=(manifest.code_revision if manifest and manifest.code_revision else None)
            or self._settings.code_revision,
            device=self._device,
            dtype=self._dtype,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------- inference

    def segment(
        self,
        image: np.ndarray,
        *,
        points: list[PointPrompt],
        box_xyxy: tuple[float, float, float, float] | None,
        previous_mask: np.ndarray | None,
        text_prompt: str | None,
    ) -> list[SegmentationProposal]:
        reason = self.unavailable_reason()
        if reason is not None:
            raise BackendUnavailableError(self.backend_id, reason)
        if not self._loaded:
            self.load()
        validate_prompts_against_capabilities(
            self.descriptor(),
            points=points,
            box_xyxy=box_xyxy,
            previous_mask=previous_mask,
            text_prompt=text_prompt,
        )
        return self._segment_unchecked(
            image,
            points=points,
            box_xyxy=box_xyxy,
            previous_mask=previous_mask,
            text_prompt=text_prompt,
        )

    def _segment_unchecked(
        self,
        image: np.ndarray,
        *,
        points: list[PointPrompt],
        box_xyxy: tuple[float, float, float, float] | None,
        previous_mask: np.ndarray | None,
        text_prompt: str | None,
    ) -> list[SegmentationProposal]:
        if text_prompt is not None:
            raise BackendCapabilityError(
                self.backend_id, BackendCapability.TEXT_PROMPTS, "SAM 2.1 has no text encoder"
            )
        rgb = self._as_rgb(image)
        height, width = image_height_width(rgb)
        if previous_mask is not None and previous_mask.shape != (height, width):
            raise ValueError(
                f"previous_mask shape {previous_mask.shape} does not match image {(height, width)}"
            )
        torch = _import("torch")
        with self._lock:
            try:
                embeddings = self._ensure_embeddings(rgb)
                kwargs: dict[str, Any] = {"original_sizes": [[height, width]]}
                if points:
                    kwargs["input_points"] = [[[[float(p.x), float(p.y)] for p in points]]]
                    kwargs["input_labels"] = [[[1 if p.positive else 0 for p in points]]]
                if box_xyxy is not None:
                    kwargs["input_boxes"] = [[[float(v) for v in box_xyxy]]]
                inputs = self._processor(return_tensors="pt", **kwargs)
                model_inputs: dict[str, Any] = {}
                for key in ("input_points", "input_labels", "input_boxes"):
                    if key in inputs:
                        model_inputs[key] = inputs[key].to(self._device)
                if previous_mask is not None:
                    model_inputs["input_masks"] = self._mask_logits(previous_mask)
                multimask = (
                    len(points) == 1
                    and points[0].positive
                    and box_xyxy is None
                    and previous_mask is None
                )
                with torch.inference_mode():
                    outputs = self._model(
                        image_embeddings=embeddings, multimask_output=multimask, **model_inputs
                    )
                    masks = self._processor.post_process_masks(
                        outputs.pred_masks.float().cpu(), [[height, width]]
                    )[0]
                    scores = outputs.iou_scores.float().cpu()
            except BackendCapabilityError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_oom(exc):
                    self._drop_embeddings()
                    self._empty_cache()
                    raise BackendOutOfMemoryError(self.backend_id, str(exc)) from exc
                raise
        masks_np = np.asarray(masks[0].numpy(), dtype=bool)  # (num_masks, H, W)
        scores_np = np.asarray(scores[0, 0].numpy(), dtype=np.float64).reshape(-1)
        order = np.argsort(-scores_np)
        proposals: list[SegmentationProposal] = []
        for index in order:
            mask = masks_np[index]
            if not mask.any():
                continue
            proposals.append(
                SegmentationProposal(
                    mask=np.ascontiguousarray(mask),
                    score=float(scores_np[index]),
                    backend_metadata={"variant": int(index), "multimask": multimask},
                )
            )
        return proposals

    def _mask_logits(self, mask: np.ndarray) -> Any:
        torch = _import("torch")
        size = tuple(self._model.prompt_encoder.mask_input_size)
        tensor = torch.from_numpy(np.asarray(mask, dtype=np.float32))[None, None]
        resized = torch.nn.functional.interpolate(
            tensor, size=size, mode="bilinear", align_corners=False, antialias=True
        )
        logits = (resized * 2.0 - 1.0) * _MASK_LOGIT
        return logits.to(self._device)

    @staticmethod
    def _as_rgb(image: np.ndarray) -> np.ndarray:
        if image.ndim == 3 and image.shape[2] == 3 and image.dtype == np.uint8:
            return np.ascontiguousarray(image)
        return to_rgb_uint8(image)

    def _ensure_embeddings(self, rgb: np.ndarray) -> Any:
        key = hashlib.sha1(rgb.tobytes()).hexdigest() + f":{rgb.shape}"
        if key == self._embedding_key and self._embeddings is not None:
            return self._embeddings
        self._drop_embeddings()
        torch = _import("torch")
        pil = _import("PIL.Image")
        inputs = self._processor(images=pil.fromarray(rgb), return_tensors="pt")
        with torch.inference_mode():
            embeddings = self._model.get_image_embeddings(inputs["pixel_values"].to(self._device))
        self._embeddings = embeddings
        self._embedding_key = key
        return embeddings

    def _drop_embeddings(self) -> None:
        self._embeddings = None
        self._embedding_key = None

    # --------------------------------------------------------------- cleanup

    def _empty_cache(self) -> None:
        try:
            torch = _import("torch")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    def mark_fatal(self, reason: str) -> None:
        self._fatal_reason = reason
        self.release()
        logger.error("sam21 backend marked unavailable", extra={"reason": reason})

    def release(self) -> None:
        with self._lock:
            self._drop_embeddings()
            self._model = None
            self._processor = None
            self._loaded = False
            self._empty_cache()
