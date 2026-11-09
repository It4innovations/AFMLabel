# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Meta SAM 3.1 backend.

Real findings against the pinned artefacts:

* ``facebook/sam3.1`` ships ``sam3.1_multiplex.pt`` whose tracker weights
  are the **Object Multiplex** tracker (``tracker.model.*``, multiplexed
  x16 shapes). They are architecturally incompatible with the SAM 3 image
  interactive predictor (``build_sam3_image_model(enable_inst_interactivity=True)``
  leaves it randomly initialised), and the 3.1 detector neck differs from
  the plain image model (``convs.3`` missing, ``interactive_convs`` extra).
* The only path that loads this checkpoint consistently is the official
  ``build_sam3_multiplex_video_predictor``. An AFM image is therefore
  handled as a one-frame session: ``start_session`` with the image, then
  ``add_prompt`` on frame 0 with points (tracker instance prompt), a box
  or text (detector prompt). Mask prompts have no equivalent in that API
  and are not advertised. The predictor requires CUDA (the upstream builder
  calls ``.cuda()``), so CPU inference is reported unavailable.

Nothing runs at import time; ``torch``/``sam3`` are imported in
:meth:`Sam31Backend.load`. A capability is advertised only after a real
synthetic inference for that prompt kind succeeded on the loaded
checkpoint. The backend never contacts the network.
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
from afmlabel.settings import RuntimeSettings, Sam31ModelSettings

logger = logging.getLogger(__name__)

BACKEND_ID = "sam31"
DISPLAY_NAME = "Meta SAM 3.1 (local)"
_DIAG_SIZE = 96
_POINT_OBJ_ID = 1


def _import(name: str) -> Any:
    return importlib.import_module(name)


def _is_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, MemoryError)
        or type(exc).__name__ in ("OutOfMemoryError", "CudaOutOfMemoryError")
        or "out of memory" in text
    )


class Sam31Backend:
    backend_id = BACKEND_ID

    def __init__(self, settings: Sam31ModelSettings, runtime: RuntimeSettings) -> None:
        self._settings = settings
        self._runtime = runtime
        self._manifest: ModelManifest | None = None
        self._loaded = False
        self._fatal_reason: str | None = None
        self._device: str | None = None
        self._dtype: str | None = None
        self._predictor: Any = None
        self._verified: set[BackendCapability] = set()
        self._capability_errors: dict[str, str] = {}
        self._lock = threading.RLock()
        self._session_id: str | None = None
        self._session_key: str | None = None
        self._load_error: str | None = None
        self._weight_report: dict[str, Any] = {}

    # ----------------------------------------------------------- availability

    def _manifest_path(self) -> Path:
        return self._settings.checkpoint_dir / MANIFEST_FILENAME

    def unavailable_reason(self) -> str | None:
        if self._fatal_reason is not None:
            return self._fatal_reason
        if not self._settings.enabled:
            return "disabled in configuration (models.sam31.enabled is false)"
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
                "python scripts/download_models.py --model sam31"
            )
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValueError) as exc:
            return f"model manifest invalid: {exc}"
        self._manifest = manifest
        if self._settings.revision and manifest.revision != self._settings.revision:
            return (
                f"manifest revision {manifest.revision} does not match the configured "
                f"models.sam31.revision {self._settings.revision}"
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
        if requested == "cpu":
            return None, (
                "the pinned SAM 3.1 multiplex predictor is CUDA-only (upstream builder "
                "moves the model to cuda); CPU inference is not available for this checkpoint"
            )
        if requested == "rocm":
            return None, "ROCm is not validated for the pinned SAM 3 stack"
        if requested not in ("auto", "cuda") and not requested.startswith("cuda:"):
            return None, f"unsupported runtime.device {requested!r}"
        if not torch.cuda.is_available():
            return None, (
                "no CUDA device is available; the pinned SAM 3.1 multiplex predictor "
                "is CUDA-only (models.sam31.allow_cpu cannot enable it)"
            )
        if requested.startswith("cuda:"):
            index = int(requested.split(":")[1])
            if index >= torch.cuda.device_count():
                return None, f"runtime.device {requested!r} does not exist"
            torch.cuda.set_device(index)
        return f"cuda:{torch.cuda.current_device()}", None

    # ------------------------------------------------------------------ load

    def _check_weights(self, checkpoint: Path) -> None:
        """Compare checkpoint keys with the built model (names only, mmap) so a
        silently random-initialised submodule cannot go unnoticed."""
        torch = _import("torch")
        try:
            ckpt = torch.load(checkpoint, map_location="cpu", weights_only=True, mmap=True)
        except TypeError:  # pragma: no cover - older torch without mmap
            ckpt = torch.load(checkpoint, map_location="cpu", weights_only=True)
        if "model" in ckpt and isinstance(ckpt["model"], dict):
            ckpt = ckpt["model"]
        ckpt_keys = set(ckpt.keys())
        model_keys = set(self._predictor.model.state_dict().keys())
        missing = sorted(model_keys - ckpt_keys)
        unexpected = sorted(ckpt_keys - model_keys)
        # Buffers created at build time (e.g. rotary tables) are not stored.
        real_missing = [k for k in missing if "freqs_cis" not in k and not k.endswith("rope_cache")]
        self._weight_report = {
            "checkpoint_keys": len(ckpt_keys),
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
                builder = _import("sam3.model_builder")
                logger.info(
                    "loading SAM 3.1 multiplex predictor", extra={"checkpoint": str(checkpoint)}
                )
                self._predictor = builder.build_sam3_multiplex_video_predictor(
                    checkpoint_path=str(checkpoint),
                    use_fa3=False,
                    use_rope_real=True,
                    compile=False,
                    warm_up=False,
                    async_loading_frames=False,
                )
                self._device = device
                param = next(self._predictor.model.parameters())
                self._dtype = str(param.dtype).replace("torch.", "") + "+bf16-autocast"
                self._check_weights(checkpoint)
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
                "SAM 3.1 loaded",
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
        checks: list[tuple[BackendCapability, dict[str, Any]]] = [
            (
                BackendCapability.POINT_PROMPTS,
                {
                    "points": [PointPrompt(48, 48, True), PointPrompt(8, 8, False)],
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
                for proposal in proposals:
                    if proposal.mask.shape != (_DIAG_SIZE, _DIAG_SIZE):
                        raise RuntimeError(f"mask shape {proposal.mask.shape}")
                    if proposal.mask.dtype != np.bool_:
                        raise RuntimeError(f"mask dtype {proposal.mask.dtype}")
                if capability == BackendCapability.POINT_PROMPTS and not proposals:
                    raise RuntimeError("no proposal returned for a positive point")
                self._verified.add(capability)
            except Exception as exc:  # noqa: BLE001
                self._capability_errors[capability.value] = f"{type(exc).__name__}: {exc}"
        self._capability_errors[BackendCapability.MASK_PROMPTS.value] = (
            "the SAM 3.1 multiplex predictor API has no mask-prompt input"
        )
        self._close_session()

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
        if previous_mask is not None:
            raise BackendCapabilityError(
                self.backend_id,
                BackendCapability.MASK_PROMPTS,
                "the SAM 3.1 multiplex predictor has no mask-prompt input",
            )
        if points and (box_xyxy is not None or text_prompt is not None):
            raise BackendCapabilityError(
                self.backend_id,
                BackendCapability.POINT_PROMPTS,
                "point prompts cannot be combined with box or text prompts in SAM 3.1",
            )
        with self._lock:
            try:
                self._ensure_session(rgb)
                self._request({"type": "reset_session", "session_id": self._session_id})
                self._prime_frame_state()
                request: dict[str, Any] = {
                    "type": "add_prompt",
                    "session_id": self._session_id,
                    "frame_index": 0,
                    "rel_coordinates": True,
                }
                path = "detector"
                if points:
                    path = "tracker"
                    request["points"] = [[p.x / width, p.y / height] for p in points]
                    request["point_labels"] = [1 if p.positive else 0 for p in points]
                    request["obj_id"] = _POINT_OBJ_ID
                else:
                    if text_prompt is not None:
                        request["text"] = text_prompt
                    if box_xyxy is not None:
                        x0, y0, x1, y1 = box_xyxy
                        request["bounding_boxes"] = [
                            [x0 / width, y0 / height, (x1 - x0) / width, (y1 - y0) / height]
                        ]
                        request["bounding_box_labels"] = [1]
                response = self._request(request)
            except BackendCapabilityError:
                raise
            except Exception as exc:  # noqa: BLE001
                if _is_oom(exc):
                    self._close_session()
                    self._empty_cache()
                    raise BackendOutOfMemoryError(self.backend_id, str(exc)) from exc
                raise
        return self._proposals_from(response, height, width, path, text_prompt)

    def _request(self, request: dict[str, Any]) -> Any:
        torch = _import("torch")
        with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            return self._predictor.handle_request(request)

    @staticmethod
    def _as_rgb(image: np.ndarray) -> np.ndarray:
        if image.ndim == 3 and image.shape[2] == 3 and image.dtype == np.uint8:
            return np.ascontiguousarray(image)
        return to_rgb_uint8(image)

    def _ensure_session(self, rgb: np.ndarray) -> None:
        key = hashlib.sha1(rgb.tobytes()).hexdigest() + f":{rgb.shape}"
        if key == self._session_key and self._session_id is not None:
            return
        self._close_session()
        pil = _import("PIL.Image")
        torch = _import("torch")
        uuid = _import("uuid")
        time = _import("time")
        # The base predictor's ``start_session`` forwards ``offload_state_to_cpu`` to
        # ``init_state``, which the multiplex model does not accept (upstream
        # mismatch at the pinned revision). Initialise the state directly and
        # register it the same way ``start_session`` does.
        with torch.inference_mode(), torch.autocast(device_type="cuda", dtype=torch.bfloat16):
            state = self._predictor.model.init_state(
                resource_path=[pil.fromarray(rgb)],
                offload_video_to_cpu=False,
                async_loading_frames=False,
            )
        session_id = str(uuid.uuid4())
        now = time.time()
        self._predictor._all_inference_states[session_id] = {  # noqa: SLF001
            "state": state,
            "session_id": session_id,
            "start_time": now,
            "last_use_time": now,
        }
        self._session_id = session_id
        self._session_key = key

    def _prime_frame_state(self) -> None:
        """Mark the session as a single image and pre-create the detector output
        slot for frame 0.

        Verified against the pinned code: ``add_sam2_new_points`` builds its
        output through ``_build_sam2_output``, which returns nothing when
        ``cached_frame_outputs`` has no entry for the frame, so a point prompt
        on a fresh session (no detector pass yet) would silently yield an
        empty result. Priming the slot makes the refined mask appear, exactly
        as it does after a detector prompt.
        """
        session = self._predictor._all_inference_states.get(self._session_id)  # noqa: SLF001
        if not session:
            return
        state = session["state"]
        state["is_image_only"] = True
        state.setdefault("cached_frame_outputs", {}).setdefault(0, {})

    def _close_session(self) -> None:
        if self._session_id is not None and self._predictor is not None:
            try:
                self._request({"type": "close_session", "session_id": self._session_id})
            except Exception:  # noqa: BLE001
                pass
        self._session_id = None
        self._session_key = None

    def _proposals_from(
        self, response: Any, height: int, width: int, path: str, text_prompt: str | None
    ) -> list[SegmentationProposal]:
        outputs = response.get("outputs", response) if isinstance(response, dict) else {}
        masks = outputs.get("out_binary_masks")
        if masks is None:
            return []
        masks_np = np.asarray(masks)
        if masks_np.ndim == 2:
            masks_np = masks_np[None]
        probs = np.asarray(outputs.get("out_probs", [])).reshape(-1)
        obj_ids = np.asarray(outputs.get("out_obj_ids", [])).reshape(-1)
        scored = probs.size == masks_np.shape[0]
        order = np.argsort(-probs) if scored else np.arange(masks_np.shape[0])
        proposals: list[SegmentationProposal] = []
        for index in order:
            mask = np.asarray(masks_np[index], dtype=bool)
            if mask.shape != (height, width):
                mask = self._resize_mask(mask, height, width)
            if not mask.any():
                continue
            proposals.append(
                SegmentationProposal(
                    mask=mask,
                    score=float(probs[index]) if scored else None,
                    backend_metadata={
                        "path": path,
                        "object_id": int(obj_ids[index])
                        if obj_ids.size == masks_np.shape[0]
                        else None,
                        "text_prompt": text_prompt,
                        "variant": int(index),
                    },
                )
            )
        return proposals

    @staticmethod
    def _resize_mask(mask: np.ndarray, height: int, width: int) -> np.ndarray:
        torch = _import("torch")
        tensor = torch.from_numpy(np.asarray(mask, dtype=np.float32))[None, None]
        resized = torch.nn.functional.interpolate(tensor, size=(height, width), mode="nearest")
        result: np.ndarray = resized[0, 0].numpy() > 0.5
        return result

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
        logger.error("sam31 backend marked unavailable", extra={"reason": reason})

    def release(self) -> None:
        with self._lock:
            self._close_session()
            predictor = self._predictor
            self._predictor = None
            self._loaded = False
            if predictor is not None:
                try:
                    predictor.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            self._empty_cache()
