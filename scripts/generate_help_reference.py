# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Generate the keyboard-shortcut table inside ``docs/user-guide.md`` from
the shortcut registry.

Run after editing ``src/afmlabel/static/help/shortcuts.json``::

    python scripts/generate_help_reference.py          # write the table
    python scripts/generate_help_reference.py --check  # exit 1 when stale

``tests/unit/test_help.py`` fails when the table is out of date.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from afmlabel.help.registry import load_shortcuts  # noqa: E402

GUIDE = REPO / "docs" / "user-guide.md"
SHORTCUT_MARKERS = ("<!-- shortcuts:begin -->", "<!-- shortcuts:end -->")


def render_shortcut_table() -> str:
    lines = ["| Keys | Scope | Action |", "| --- | --- | --- |"]
    for shortcut in load_shortcuts():
        lines.append(f"| `{shortcut['keys']}` | {shortcut['scope']} | {shortcut['description']} |")
    return "\n".join(lines)


def merged_shortcuts_doc(current: str) -> str:
    begin, end = SHORTCUT_MARKERS
    if begin not in current or end not in current:
        raise SystemExit(f"{GUIDE.relative_to(REPO)} must contain {begin} and {end}")
    head, rest = current.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{render_shortcut_table()}\n{end}{tail}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 when the table is stale")
    args = parser.parse_args(argv)
    current = GUIDE.read_text(encoding="utf-8")
    merged = merged_shortcuts_doc(current)
    if args.check:
        if current != merged:
            print(f"stale: {GUIDE.relative_to(REPO)}")
            return 1
        return 0
    GUIDE.write_text(merged, encoding="utf-8")
    print(f"wrote shortcut table into {GUIDE.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
