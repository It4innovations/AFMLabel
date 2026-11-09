# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Meta SAM 3 image backend (``facebook/sam3``, ``sam3.pt``).

Unlike the 3.1 multiplex release (see :mod:`afmlabel.segmentation.sam31`),
the SAM 3 image checkpoint contains both the concept detector
(``detector.*``) and the SAM 2-style instance tracker (``tracker.*``) that
the official ``build_sam3_image_model(enable_inst_interactivity=True)``
expects, so every supported prompt kind maps to an official
API of the pinned code:

* point, box and mask prompts and the three multi-mask proposals go to
  ``model.predict_inst`` (the SAM 1 task predictor fed with the shared
  backbone output; the tracker inside the image model has no backbone of
  its own, so ``inst_interactive_predictor.set_image`` cannot be used);
* text prompts (optionally with a box exemplar) go to ``Sam3Processor``
  (``set_image`` / ``set_text_prompt`` / ``add_geometric_prompt``), the
  concept detector that returns every matching instance.

The backbone output (``Sam3Processor.set_image`` state) is computed once
per distinct image (hash of the 8-bit RGB render) and shared by both paths.
Weights are verified after the build: any parameter the checkpoint does not
provide (apart from rotary tables built at run time) makes the backend
refuse to load. Every capability is advertised only after
a real synthetic inference of that prompt kind succeeded. Nothing runs at
import time and the backend never contacts the network.
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
from afmlabel.settings import RuntimeSettings, Sam3ModelSettings

logger = logging.getLogger(__name__)

BACKEND_ID = "sam3"
DISPLAY_NAME = "Meta SAM 3 image model (local)"
_DIAG_SIZE = 96
_MASK_INPUT_SIZE = 288  # fallback only; read from the prompt encoder when loaded
_MASK_LOGIT = 10.0


def _import(name: str) -> Any:
    return importlib.import_module(name)


def _addmm_act_fp32(activation: Any, linear: Any, mat1: Any) -> Any:
    """Plain ``activation(linear(x))`` in the model's own dtype.

    The pinned code's ``sam3.perflib.fused.addmm_act`` (used by every ViT
    MLP block) casts inputs and weights to bfloat16 unconditionally. Under
    the CUDA bf16 autocast that is a no-op, but on CPU the following ``fc2``
    layer stays float32 and PyTorch raises ``mat1 and mat2 must have the
    same dtype``. On CPU the backend therefore substitutes this float32
    implementation (verified equivalent up to bf16 rounding).
    """
    torch = _import("torch")
    y = torch.nn.functional.linear(mat1, linear.weight, linear.bias)
    if activation in (torch.nn.functional.relu, torch.nn.ReLU):
        return torch.nn.functional.relu(y)
    if activation in (torch.nn.functional.gelu, torch.nn.GELU):
        return torch.nn.functional.gelu(y)
    raise ValueError(f"Unexpected activation {activation}")


def _install_cpu_mlp_path() -> None:
    vitdet = _import("sam3.model.vitdet")
    if getattr(vitdet.addmm_act, "__name__", "") != _addmm_act_fp32.__name__:
        vitdet.addmm_act = _addmm_act_fp32
        logger.info("sam3: CPU float32 MLP path installed (bf16 fused addmm bypassed)")


def _install_cpu_build_compat() -> None:
    """Make the pinned image-model builder usable on a CPU device.

    Two constructors create tensors on ``"cuda"`` unconditionally:
    ``TransformerDecoder`` caches box-relative-position coordinates
    (``_get_coords(..., device="cuda")``) and ``PositionEmbeddingSine``
    precomputes its cache with ``torch.zeros(..., device="cuda")``. On a
    CPU-only host the build crashes; on a CUDA host the cached tensors stay
    on the GPU after ``model.to("cpu")`` and the concept detector fails with
    a device mismatch. Both caches are rebuilt lazily on the input device, so
    the fix is to skip the eager precompute (position encoding) and to route
    the coordinate helper to CPU. Applied once per process, before the build.
    """
    decoder = _import("sam3.model.decoder")
    position_encoding = _import("sam3.model.position_encoding")
    decoder_cls = decoder.TransformerDecoder
    if not getattr(decoder_cls, "_afmlabel_cpu_compat", False):
        original_get_coords = decoder_cls._get_coords

        def get_coords_cpu(height: int, width: int, device: Any) -> Any:
            if device == "cuda" or getattr(device, "type", None) == "cuda":
                device = "cpu"
            return original_get_coords(height, width, device)

        decoder_cls._get_coords = staticmethod(get_coords_cpu)
        decoder_cls._afmlabel_cpu_compat = True
    pe_cls = position_encoding.PositionEmbeddingSine
    if not getattr(pe_cls, "_afmlabel_cpu_compat", False):
        original_init = pe_cls.__init__

        def init_without_precompute(self: Any, *args: Any, **kwargs: Any) -> None:
            kwargs["precompute_resolution"] = None
            original_init(self, *args, **kwargs)

        pe_cls.__init__ = init_without_precompute
        pe_cls._afmlabel_cpu_compat = True
    torch = _import("torch")
    if not torch.cuda.is_available() and not getattr(torch.Tensor, "_afmlabel_pin_shim", False):
        # The geometry encoder pins a 4-element scale tensor before moving it
        # to the device (``scale.pin_memory().to(...)``); without a CUDA
        # runtime PyTorch raises "No CUDA GPUs are available". Pinning is
        # meaningless on a CPU-only host, so it becomes a no-op there.
        original_pin = torch.Tensor.pin_memory

        def pin_memory_noop(self: Any, *args: Any, **kwargs: Any) -> Any:
            return self

        pin_memory_noop._original = original_pin  # type: ignore[attr-defined]
        torch.Tensor.pin_memory = pin_memory_noop
        torch.Tensor._afmlabel_pin_shim = True
    logger.info("sam3: CPU build compatibility installed (lazy coordinate/position caches)")


def _reset_device_caches(model: Any) -> None:
    """Drop eagerly built caches so they are recreated on the model's device."""
    for module in model.modules():
        if hasattr(module, "compilable_cord_cache"):
            module.compilable_cord_cache = None
            module.compilable_stored_size = None
            if isinstance(getattr(module, "coord_cache", None), dict):
                module.coord_cache.clear()
        cache = getattr(module, "cache", None)
        if isinstance(cache, dict) and type(module).__name__ == "PositionEmbeddingSine":
            cache.clear()


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, MemoryError)
        or type(exc).__name__ in ("OutOfMemoryError", "CudaOutOfMemoryError")
        or "out of memory" in text
    )


class Sam3Backend:
    backend_id = BACKEND_ID

    def __init__(self, settings: Sam3ModelSettings, runtime: RuntimeSettings) -> None:
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
        self._detector_key: str | None = None
        self._detector_state: dict[str, Any] | None = None
        self._weight_report: dict[str, Any] = {}

    # ----------------------------------------------------------- availability

    def _manifest_path(self) -> Path:
        return self._settings.checkpoint_dir / MANIFEST_FILENAME

    def unavailable_reason(self) -> str | None:
        if self._fatal_reason is not None:
            return self._fatal_reason
        if not self._settings.enabled:
            return "disabled in configuration (models.sam3.enabled is false)"
        if importlib.util.find_spec("torch") is None:
            return "PyTorch is not installed; install the optional 'sam' extra"
        if importlib.util.find_spec("sam3") is None:
            return (
                "the pinned official SAM 3 package is not installed; "
                "see README.md (Optional: local SAM backends)"
            )
        manifest_path = self._manifest_path()
        if not manifest_path.exists():
            return (
                f"model manifest not found at {manifest_path}; run "
                "python scripts/download_models.py --model sam3"
            )
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            return f"model manifest invalid: {exc}"
        self._manifest = manifest
        if self._settings.revision and manifest.revision != self._settings.revision:
            return (
                f"manifest revision {manifest.revision} does not match the configured "
                f"models.sam3.revision {self._settings.revision}"
            )
        if manifest.repository != self._settings.repository:
            return (
                f"manifest repository {manifest.repository} does not match configured "
                f"{self._settings.repository}"
            )
        if manifest.checkpoint_file is None:
            return "model manifest does not identify a checkpoint file"
        if self._load_error is not None:
            return self._load_error
        return None

    def _select_device(self) -> tuple[str | None, str | None]:
        torch = _import("torch")
        requested = (self._runtime.device or "auto").lower()
        if requested == "rocm":
            return None, "ROCm is not validated for the pinned SAM 3 stack"
        if requested == "cpu":
            if not self._settings.allow_cpu:
                return None, "runtime.device is cpu but models.sam3.allow_cpu is false"
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
            return None, (
                "no CUDA device is available and models.sam3.allow_cpu is false "
                "(the 3.4 GB SAM 3 image model is impractically slow on CPU)"
            )
        return None, f"runtime.device {requested!r} requested but no CUDA device is available"

    # ------------------------------------------------------------------ load

    def _check_weights(self, checkpoint: Path) -> None:
        """Replicate the upstream key mapping (``detector.`` stripped, ``tracker.``
        -> ``inst_interactive_predictor.model.``) and refuse to run when any
        model parameter would stay randomly initialised."""
        torch = _import("torch")
        try:
            ckpt = torch.load(checkpoint, map_location="cpu", weights_only=True, mmap=True)
        except TypeError:  # pragma: no cover - older torch without mmap
            ckpt = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if "model" in ckpt and isinstance(ckpt["model"], dict):
            ckpt = ckpt["model"]
        mapped: set[str] = set()
        for key in ckpt:
            if "detector" in key:
                mapped.add(key.replace("detector.", ""))
            if "tracker" in key:
                mapped.add(key.replace("tracker.", "inst_interactive_predictor.model."))
        model_keys = set(self._model.state_dict().keys())
        missing = sorted(model_keys - mapped)
        unexpected = sorted(mapped - model_keys)
        real_missing = [k for k in missing if "freqs_cis" not in k and not k.endswith("rope_cache")]
        self._weight_report = {
            "checkpoint_keys": len(ckpt),
            "model_keys": len(model_keys),
            "missing": real_missing[:20],
            "missing_count": len(real_missing),
            "unexpected_count": len(unexpected),
        }
        del ckpt
        if real_missing:
            raise RuntimeError(
                f"{len(real_missing)} model parameter(s) are not in the checkpoint "
                f"(e.g. {real_missing[:3]}); refusing to run with random weights"
            )

    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            reason = self.unavailable_reason()
            if reason is not None:
                raise BackendUnavailableError(self.backend_id, reason)
            manifest = self._manifest
            assert manifest is not None and manifest.checkpoint_file is not None
            root = self._settings.checkpoint_dir
            problems = verify_manifest(manifest, root)
            if problems:
                self._load_error = f"MODEL_CHECKPOINT_INVALID: {'; '.join(problems[:3])}"
                raise BackendUnavailableError(self.backend_id, self._load_error)
            device, device_reason = self._select_device()
            if device is None:
                self._load_error = device_reason
                raise BackendUnavailableError(self.backend_id, device_reason or "no device")
            checkpoint = root / manifest.checkpoint_file
            try:
                torch = _import("torch")
                builder = _import("sam3.model_builder")
                processor_module = _import("sam3.model.sam3_image_processor")
                logger.info("loading SAM 3 image model", extra={"checkpoint": str(checkpoint)})
                if device == "cpu":
                    _install_cpu_build_compat()
                model = builder.build_sam3_image_model(
                    checkpoint_path=str(checkpoint),
                    load_from_HF=False,
                    enable_segmentation=True,
                    enable_inst_interactivity=True,
                    device="cpu",
                    eval_mode=True,
                    compile=False,
                )
                self._model = model
                self._check_weights(checkpoint)
                if device == "cpu":
                    _install_cpu_mlp_path()
                model.to(torch.device(device))
                if device == "cpu":
                    _reset_device_caches(model)
                self._processor = processor_module.Sam3Processor(model, device=device)
                self._device = device
                param = next(model.parameters())
                base = str(param.dtype).replace("torch.", "")
                self._dtype = base + ("+bf16-autocast" if device.startswith("cuda") else "")
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
                "SAM 3 loaded",
                extra={
                    "device": self._device,
                    "capabilities": sorted(c.value for c in self._verified),
                    "weights": self._weight_report,
                },
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
            (
                BackendCapability.TEXT_PROMPTS,
                {"points": [], "box_xyxy": None, "previous_mask": None, "text_prompt": "blob"},
            ),
        ]
        for capability, kwargs in checks:
            try:
                proposals = self._segment_unchecked(image, **kwargs)
                self._assert_proposals(proposals, allow_empty=capability is checks[2][0])
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
        self._drop_caches()

    @staticmethod
    def _assert_proposals(
        proposals: list[SegmentationProposal], *, allow_empty: bool = False
    ) -> None:
        if not proposals and not allow_empty:
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
        rgb = self._as_rgb(image)
        height, width = image_height_width(rgb)
        if previous_mask is not None and previous_mask.shape != (height, width):
            raise ValueError(
                f"previous_mask shape {previous_mask.shape} does not match image {(height, width)}"
            )
        if text_prompt is not None and (points or previous_mask is not None):
            raise BackendCapabilityError(
                self.backend_id,
                BackendCapability.TEXT_PROMPTS,
                "a text prompt (concept detector) cannot be combined with point or mask "
                "prompts (instance predictor); a box may accompany the text as an exemplar",
            )
        with self._lock:
            try:
                if text_prompt is not None:
                    return self._segment_text(rgb, height, width, text_prompt, box_xyxy)
                return self._segment_interactive(
                    rgb, height, width, points, box_xyxy, previous_mask
                )
            except BackendCapabilityError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_oom(exc):
                    self._drop_caches()
                    self._empty_cache()
                    raise BackendOutOfMemoryError(self.backend_id, str(exc)) from exc
                raise

    def _autocast(self) -> Any:
        torch = _import("torch")
        if self._device and self._device.startswith("cuda"):
            return torch.autocast(device_type="cuda", dtype=torch.bfloat16)
        return torch.autocast(device_type="cpu", enabled=False)

    def _segment_interactive(
        self,
        rgb: np.ndarray,
        height: int,
        width: int,
        points: list[PointPrompt],
        box_xyxy: tuple[float, float, float, float] | None,
        previous_mask: np.ndarray | None,
    ) -> list[SegmentationProposal]:
        torch = _import("torch")
        state = self._ensure_state(rgb)
        point_coords = (
            np.asarray([[p.x, p.y] for p in points], dtype=np.float32) if points else None
        )
        point_labels = (
            np.asarray([1 if p.positive else 0 for p in points], dtype=np.int32) if points else None
        )
        box = np.asarray(box_xyxy, dtype=np.float32) if box_xyxy is not None else None
        mask_input = self._mask_logits(previous_mask) if previous_mask is not None else None
        multimask = len(points) == 1 and points[0].positive and box is None and mask_input is None
        # The image model's tracker is built without its own backbone; the
        # official entry point is ``predict_inst`` which feeds the shared
        # backbone output of ``Sam3Processor.set_image`` to the predictor.
        with torch.inference_mode(), self._autocast():
            masks, scores, _ = self._model.predict_inst(
                state,
                point_coords=point_coords,
                point_labels=point_labels,
                box=box,
                mask_input=mask_input,
                multimask_output=multimask,
                return_logits=False,
                normalize_coords=True,
            )
        masks_np = np.asarray(masks)
        if masks_np.ndim == 2:
            masks_np = masks_np[None]
        scores_np = np.asarray(scores, dtype=np.float64).reshape(-1)
        scored = scores_np.size == masks_np.shape[0]
        order = np.argsort(-scores_np) if scored else np.arange(len(masks_np))
        proposals: list[SegmentationProposal] = []
        for index in order:
            mask = masks_np[index] > 0.5
            if mask.shape != (height, width):
                mask = self._resize_mask(mask, height, width)
            if not mask.any():
                continue
            proposals.append(
                SegmentationProposal(
                    mask=np.ascontiguousarray(mask, dtype=bool),
                    score=float(scores_np[index]) if scored else None,
                    backend_metadata={
                        "path": "instance_predictor",
                        "variant": int(index),
                        "multimask": multimask,
                    },
                )
            )
        return proposals

    def _segment_text(
        self,
        rgb: np.ndarray,
        height: int,
        width: int,
        text_prompt: str,
        box_xyxy: tuple[float, float, float, float] | None,
    ) -> list[SegmentationProposal]:
        torch = _import("torch")
        state = self._ensure_state(rgb)
        with torch.inference_mode(), self._autocast():
            self._processor.reset_all_prompts(state)
            state = self._processor.set_text_prompt(text_prompt, state)
            if box_xyxy is not None:
                x0, y0, x1, y1 = box_xyxy
                box_cxcywh = [
                    (x0 + x1) / 2 / width,
                    (y0 + y1) / 2 / height,
                    (x1 - x0) / width,
                    (y1 - y0) / height,
                ]
                state = self._processor.add_geometric_prompt(box_cxcywh, True, state)
            masks = state["masks"].detach().float().cpu().numpy()
            scores = state["scores"].detach().float().cpu().numpy().reshape(-1)
        masks_np = np.asarray(masks)
        if masks_np.ndim == 4:
            masks_np = masks_np[:, 0]
        order = np.argsort(-scores) if scores.size == masks_np.shape[0] else range(len(masks_np))
        proposals: list[SegmentationProposal] = []
        for index in order:
            mask = masks_np[index] > 0.5
            if mask.shape != (height, width):
                mask = self._resize_mask(mask, height, width)
            if not mask.any():
                continue
            proposals.append(
                SegmentationProposal(
                    mask=np.ascontiguousarray(mask, dtype=bool),
                    score=float(scores[index]) if scores.size == masks_np.shape[0] else None,
                    backend_metadata={
                        "path": "concept_detector",
                        "variant": int(index),
                        "text_prompt": text_prompt,
                        "box_exemplar": box_xyxy is not None,
                    },
                )
            )
        return proposals

    def _mask_input_size(self) -> tuple[int, int]:
        """Low-resolution mask prompt grid: 4x the image embedding (1008 / 14 * 4
        = 288 for SAM 3, not the SAM 1/2 default of 256)."""
        try:
            size = self._model.inst_interactive_predictor.model.sam_prompt_encoder.mask_input_size
            return int(size[0]), int(size[1])
        except AttributeError:
            return _MASK_INPUT_SIZE, _MASK_INPUT_SIZE

    def _mask_logits(self, mask: np.ndarray) -> np.ndarray:
        torch = _import("torch")
        tensor = torch.from_numpy(np.asarray(mask, dtype=np.float32))[None, None]
        resized = torch.nn.functional.interpolate(
            tensor,
            size=self._mask_input_size(),
            mode="bilinear",
            align_corners=False,
            antialias=True,
        )
        logits: np.ndarray = ((resized[0] * 2.0 - 1.0) * _MASK_LOGIT).numpy()
        return logits  # 1 x 256 x 256

    @staticmethod
    def _image_key(rgb: np.ndarray) -> str:
        return hashlib.sha1(rgb.tobytes()).hexdigest() + f":{rgb.shape}"

    def _ensure_state(self, rgb: np.ndarray) -> dict[str, Any]:
        """One backbone pass per distinct image, shared by the concept detector
        (text/box exemplar) and the instance predictor (points/box/mask)."""
        key = self._image_key(rgb)
        if key == self._detector_key and self._detector_state is not None:
            return self._detector_state
        self._drop_caches()
        torch = _import("torch")
        pil = _import("PIL.Image")
        with torch.inference_mode(), self._autocast():
            state = self._processor.set_image(pil.fromarray(rgb))
        self._detector_state = state
        self._detector_key = key
        return state

    @staticmethod
    def _as_rgb(image: np.ndarray) -> np.ndarray:
        if image.ndim == 3 and image.shape[2] == 3 and image.dtype == np.uint8:
            return np.ascontiguousarray(image)
        return to_rgb_uint8(image)

    @staticmethod
    def _resize_mask(mask: np.ndarray, height: int, width: int) -> np.ndarray:
        torch = _import("torch")
        tensor = torch.from_numpy(np.asarray(mask, dtype=np.float32))[None, None]
        resized = torch.nn.functional.interpolate(tensor, size=(height, width), mode="nearest")
        result: np.ndarray = resized[0, 0].numpy() > 0.5
        return result

    def _drop_caches(self) -> None:
        self._detector_key = None
        self._detector_state = None
        if self._model is not None:
            try:
                self._model.inst_interactive_predictor.reset_predictor()
            except Exception:  # noqa: BLE001
                pass

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
        logger.error("sam3 backend marked unavailable", extra={"reason": reason})

    def release(self) -> None:
        with self._lock:
            self._drop_caches()
            self._model = None
            self._processor = None
            self._loaded = False
            self._empty_cache()
