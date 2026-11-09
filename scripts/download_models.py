#!/usr/bin/env python
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Download a pinned model checkpoint from Hugging Face and write the local
manifest. Never run by the application itself.

    export HF_TOKEN=...            # PowerShell: $env:HF_TOKEN = "..."
    python scripts/download_models.py --model sam31   # facebook/sam3.1 (gated)
    python scripts/download_models.py --model sam3    # facebook/sam3 (gated)
    python scripts/download_models.py --model sam21   # facebook/sam2.1-hiera-base-plus
    python scripts/download_models.py --model sam31 --revision <commit> --dest ./models/sam31

The script:
  1. verifies HF_TOKEN is available when the repository is gated;
  2. downloads the pinned revision of the configured repository;
  3. validates that the expected files are present;
  4. computes SHA-256 hashes and writes ``manifest.json``;
  5. transmits nothing but the token and the file requests.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from afmlabel.privacy.redaction import register_secret  # noqa: E402
from afmlabel.segmentation.manifest import (  # noqa: E402
    MANIFEST_FILENAME,
    build_manifest,
    verify_manifest,
    write_manifest,
)
from afmlabel.settings import load_settings  # noqa: E402

MODELS = {
    "sam31": {
        "repository": "facebook/sam3.1",
        # Exact commit of the Hugging Face repository verified on 2026-09-03.
        "revision": "daa63191845a41281374e725f4c9e51c7a824460",
        "expected_files": [
            "sam3.1_multiplex.pt",
            "config.json",
            "processor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.json",
            "merges.txt",
            "LICENSE",
        ],
        # Official code revision (facebookresearch/sam3) the checkpoint is
        # validated against; keep in sync with config/app.example.yaml.
        "code_revision": "660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b7",
        "license_note": (
            "SAM License (see LICENSE in the checkpoint directory); gated access on Hugging Face"
        ),
        "gated": True,
    },
    "sam3": {
        "repository": "facebook/sam3",
        # SAM 3 image checkpoint (2025-11-20 release), commit verified on 2026-09-04.
        "revision": "3c879f39826c281e95690f02c7821c4de09afae7",
        "expected_files": [
            "sam3.pt",
            "config.json",
            "processor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "vocab.json",
            "merges.txt",
            "LICENSE",
        ],
        "code_revision": "660a5e9e1b8b4c02c0ad97229b88a09a6e4ff5b7",
        "license_note": (
            "SAM License (see LICENSE in the checkpoint directory); gated access on Hugging Face"
        ),
        "gated": True,
    },
    "sam21": {
        "repository": "facebook/sam2.1-hiera-base-plus",
        # transformers-format SAM 2.1 base-plus snapshot, commit verified on 2026-09-04.
        "revision": "b7320756a13354e7530a63935656d35b2f91a290",
        "expected_files": [
            "config.json",
            "model.safetensors",
            "preprocessor_config.json",
            "processor_config.json",
        ],
        # Consumed through the Hugging Face transformers port; the code revision
        # recorded in the manifest is the installed transformers version.
        "code_revision": None,
        "license_note": "Apache-2.0 (SAM 2.1 model card); not gated",
        "gated": False,
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", choices=sorted(MODELS), required=True)
    parser.add_argument("--revision", default=None, help="override the pinned Hugging Face commit")
    parser.add_argument(
        "--dest", type=Path, default=None, help="destination directory (default from config)"
    )
    parser.add_argument("--config", type=Path, default=None, help="path to config/app.yaml")
    parser.add_argument(
        "--verify-only", action="store_true", help="only verify an existing manifest"
    )
    args = parser.parse_args(argv)

    spec = MODELS[args.model]
    settings = load_settings(args.config)
    model_settings = getattr(settings.models, args.model)
    revision = args.revision or model_settings.revision or spec["revision"]
    dest = args.dest or model_settings.checkpoint_dir
    dest = Path(dest)

    if args.verify_only:
        from afmlabel.segmentation.manifest import load_manifest

        manifest = load_manifest(dest / MANIFEST_FILENAME)
        problems = verify_manifest(manifest, dest)
        print(f"manifest: {dest / MANIFEST_FILENAME}")
        print(f"repository: {manifest.repository} @ {manifest.revision}")
        print(f"checkpoint sha256: {manifest.checkpoint_sha256}")
        for problem in problems:
            print(f"  PROBLEM: {problem}")
        return 1 if problems else 0

    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token and spec["gated"]:
        print(
            f"HF_TOKEN_MISSING: set HF_TOKEN in the environment (the {spec['repository']} "
            f"repository is gated; request access at https://huggingface.co/{spec['repository']} "
            "first)",
            file=sys.stderr,
        )
        return 2
    if token:
        register_secret(token)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("huggingface_hub is not installed: pip install -e '.[sam]'", file=sys.stderr)
        return 2

    print(f"downloading {spec['repository']} @ {revision} -> {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(
        repo_id=spec["repository"],
        revision=revision,
        local_dir=str(dest),
        token=token or None,
        allow_patterns=spec["expected_files"] + ["README.md"],
    )
    missing = [name for name in spec["expected_files"] if not (dest / name).exists()]
    if missing:
        print(
            f"MODEL_CHECKPOINT_INVALID: expected files missing after download: {missing}",
            file=sys.stderr,
        )
        return 1
    code_revision = model_settings.code_revision or spec["code_revision"]
    if code_revision is None and args.model == "sam21":
        try:
            import transformers

            code_revision = f"transformers=={transformers.__version__}"
        except ImportError:
            code_revision = None
    manifest = build_manifest(
        model_id=args.model,
        repository=spec["repository"],
        revision=revision,
        root=dest,
        code_revision=code_revision,
        license_note=spec["license_note"],
    )
    path = write_manifest(manifest, dest)
    problems = verify_manifest(manifest, dest)
    print(f"manifest written: {path}")
    print(f"checkpoint: {manifest.checkpoint_file} sha256 {manifest.checkpoint_sha256}")
    print(f"code revision: {manifest.code_revision}")
    if problems:
        for problem in problems:
            print(f"  PROBLEM: {problem}", file=sys.stderr)
        return 1
    print("configure config/app.yaml:")
    print(f"  models:\n    {args.model}:\n      enabled: true")
    print(f"      revision: {revision}")
    print(f"      checkpoint_dir: {dest}")
    print(f"      code_revision: {manifest.code_revision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
