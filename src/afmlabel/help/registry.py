# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Loader for the centralised contextual-help registry and the shortcut
registry.

Both registries are plain JSON files under ``static/help`` so the browser
loads them without a build step, the documentation generator
(``scripts/generate_help_reference.py``) renders the shortcut table into
``docs/user-guide.md`` and the tests can verify that every ``data-help``
key used by a template exists.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from afmlabel.templating import STATIC_DIR, TEMPLATE_DIR

HELP_DIR = STATIC_DIR / "help"
REGISTRY_FILE = HELP_DIR / "registry.json"
SHORTCUTS_FILE = HELP_DIR / "shortcuts.json"

#: Allowed ``kind`` values: what an action changes.
KINDS = frozenset({"canonical", "view", "review", "validation", "navigation", "destructive"})

_DATA_HELP = re.compile(r'data-help="([a-z0-9_.-]+)"')


@lru_cache(maxsize=1)
def load_registry() -> dict[str, dict[str, Any]]:
    data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, Any]] = data["entries"]
    return entries


@lru_cache(maxsize=1)
def load_shortcuts() -> list[dict[str, Any]]:
    data = json.loads(SHORTCUTS_FILE.read_text(encoding="utf-8"))
    shortcuts: list[dict[str, Any]] = data["shortcuts"]
    return shortcuts


def template_help_keys(template_dir: Path = TEMPLATE_DIR) -> dict[str, set[str]]:
    """``data-help`` keys used by each template (and JS module)."""
    used: dict[str, set[str]] = {}
    for path in sorted(list(template_dir.glob("*.html")) + list((STATIC_DIR / "js").glob("*.js"))):
        keys = set(_DATA_HELP.findall(path.read_text(encoding="utf-8")))
        if keys:
            used[path.name] = keys
    return used


def validate_registry(entries: dict[str, dict[str, Any]]) -> list[str]:
    """Return human-readable problems; an empty list means the registry is sound."""
    problems: list[str] = []
    for key, entry in entries.items():
        for required in ("screen", "label", "short", "kind"):
            if not entry.get(required):
                problems.append(f"{key}: missing {required!r}")
        if entry.get("kind") not in KINDS:
            problems.append(f"{key}: unknown kind {entry.get('kind')!r}")
        short = str(entry.get("short", "")).strip()
        label = str(entry.get("label", "")).strip()
        if short and label and short.casefold() == label.casefold():
            problems.append(f"{key}: short description merely repeats the label")
        if short and len(short) < 12:
            problems.append(f"{key}: short description is too short to explain anything")
        docs = entry.get("docs")
        if docs and not re.match(r"^[a-z0-9-]+(#[a-z0-9-]+)?$", str(docs)):
            problems.append(f"{key}: docs anchor {docs!r} must look like 'page#anchor'")
    return problems
