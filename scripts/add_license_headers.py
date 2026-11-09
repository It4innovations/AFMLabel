# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Add (or verify) the MIT licence header in every source file.

Usage::

    python scripts/add_license_headers.py          # add missing headers
    python scripts/add_license_headers.py --check  # exit 1 when a header is missing

The header is two comment lines (SPDX identifier + copyright) in the comment
syntax of each file type. Shebangs, XML declarations and ``<!doctype>`` lines
stay first. ``tests/unit/test_license_headers.py`` runs the ``--check`` mode.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SPDX = "SPDX-License-Identifier: MIT"
COPYRIGHT = "Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava"

#: Directories scanned for source files (relative to the repository root).
SOURCE_DIRS = ("src", "tests", "scripts", "config")
#: Top-level files that also carry a header.
TOP_LEVEL = ("pyproject.toml",)

#: suffix -> (line prefix, block open, block close); ``None`` prefix = block comment.
STYLES: dict[str, tuple[str | None, str, str]] = {
    ".py": ("# ", "", ""),
    ".ps1": ("# ", "", ""),
    ".sh": ("# ", "", ""),
    ".toml": ("# ", "", ""),
    ".yaml": ("# ", "", ""),
    ".yml": ("# ", "", ""),
    ".js": ("// ", "", ""),
    ".css": (None, "/*", " */"),
    ".html": (None, "{#", " #}"),
    ".svg": (None, "<!--", " -->"),
}

SKIP_DIRS = {"__pycache__", ".venv", "node_modules", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
#: Formats without comments (JSON, images, data) are left alone.
SKIP_SUFFIXES = {".json", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".ico", ".md", ".txt", ".lock"}


def header_lines(suffix: str) -> list[str]:
    prefix, opener, closer = STYLES[suffix]
    if prefix is not None:
        return [f"{prefix}{SPDX}", f"{prefix}{COPYRIGHT}"]
    return [f"{opener} {SPDX}", f"   {COPYRIGHT}{closer}"]


def iter_source_files() -> Iterable[Path]:
    for name in TOP_LEVEL:
        path = REPO / name
        if path.is_file():
            yield path
    for directory in SOURCE_DIRS:
        base = REPO / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix in STYLES and path.suffix not in SKIP_SUFFIXES:
                yield path


def has_header(text: str) -> bool:
    head = "\n".join(text.splitlines()[:6])
    return SPDX in head and COPYRIGHT in head


def add_header(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    if has_header(text):
        return False
    lines = text.splitlines(keepends=True)
    insert_at = 0
    # Keep shebang / XML declaration / doctype on the first line.
    if lines and (
        lines[0].startswith("#!")
        or lines[0].lstrip().lower().startswith("<?xml")
        or lines[0].lstrip().lower().startswith("<!doctype")
    ):
        insert_at = 1
    header = "".join(f"{line}\n" for line in header_lines(path.suffix))
    new_text = "".join(lines[:insert_at]) + header + "".join(lines[insert_at:])
    if not text.endswith("\n") and text:
        new_text = new_text.rstrip("\n") + "\n"
    path.write_text(new_text, encoding="utf-8", newline="\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only report files without a header")
    args = parser.parse_args(argv)
    missing: list[Path] = []
    changed = 0
    for path in iter_source_files():
        if args.check:
            if not has_header(path.read_text(encoding="utf-8")):
                missing.append(path)
        elif add_header(path):
            changed += 1
    if args.check:
        for path in missing:
            print(f"missing licence header: {path.relative_to(REPO)}")
        print(f"{len(missing)} file(s) without a licence header")
        return 1 if missing else 0
    print(f"added licence header to {changed} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
