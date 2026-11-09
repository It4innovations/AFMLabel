# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Help subsystem: registry soundness, template coverage, Markdown renderer
and shortcut registry."""

from __future__ import annotations

import json
import re
from pathlib import Path

from afmlabel.help.markdown import render_markdown, rewrite_link, slugify
from afmlabel.help.registry import (
    REGISTRY_FILE,
    load_registry,
    load_shortcuts,
    template_help_keys,
    validate_registry,
)

REPO = Path(__file__).resolve().parents[2]


def test_registry_is_sound() -> None:
    entries = load_registry()
    assert len(entries) > 100
    assert validate_registry(entries) == []


def test_every_template_help_key_exists_in_registry() -> None:
    entries = load_registry()
    missing: dict[str, set[str]] = {}
    for name, keys in template_help_keys().items():
        unknown = {k for k in keys if k not in entries}
        if unknown:
            missing[name] = unknown
    assert missing == {}


def test_required_help_wording(tmp_path: Path) -> None:
    entries = load_registry()
    assert (
        "view-only" in entries["view.brightness"]["short"].lower()
        or "only the displayed" in entries["view.brightness"]["short"]
    )
    assert "not saved" in entries["view.brightness"]["short"]
    assert "only the displayed" in entries["view.contrast"]["short"]
    assert "Prevent geometry edits" in entries["regions.lock"]["short"]
    assert "review state only" in entries["findings.mark_selected"]["short"]
    assert "never subtracts" in entries["findings.mark_selected"]["short"]
    assert "machine mask stays scalar" in entries["export.color_visualization"]["short"]
    assert "Undoable" in entries["regions.subtract"]["short"]
    assert "confirmation" in entries["regions.delete_selected"]["short"]
    assert "undo" in entries["regions.merge"]["short"].lower()
    for key in (
        "regions.subtract",
        "regions.merge",
        "regions.split",
        "findings.mark_selected",
        "view.brightness",
        "lossy.target_class",
        "export.profile",
    ):
        assert entries[key].get("docs"), f"{key} must link to local documentation"


def test_docs_anchors_resolve_to_existing_pages_and_headings() -> None:
    entries = load_registry()
    docs = REPO / "docs"
    for key, entry in entries.items():
        target = entry.get("docs")
        if not target:
            continue
        page, _, anchor = target.partition("#")
        path = docs / f"{page}.md"
        assert path.exists(), f"{key}: docs page {page} missing"
        if anchor:
            rendered = render_markdown(path.read_text(encoding="utf-8"))
            anchors = {h.anchor for h in rendered.headings}
            assert anchor in anchors, f"{key}: anchor #{anchor} not in {page}.md"


def test_shortcut_registry_matches_documentation() -> None:
    shortcuts = load_shortcuts()
    ids = [s["id"] for s in shortcuts]
    assert len(ids) == len(set(ids))
    text = (REPO / "docs" / "user-guide.md").read_text(encoding="utf-8")
    for shortcut in shortcuts:
        assert shortcut["keys"] in text, f"{shortcut['id']} ({shortcut['keys']}) not documented"
    # Registry shortcuts referenced from help entries must exist in the table.
    keys = {s["keys"] for s in shortcuts}
    registry = load_registry()
    for key, entry in registry.items():
        sc = entry.get("shortcut")
        if sc and sc not in (
            "Esc",
            "Delete",
            "Enter",
            "Ctrl+A",
            "[ / ]",
            "1-9 / Tab / Shift+Tab",
            "1-9, 0 = 10",
            "+",
            "-",
            "R",
        ):
            assert any(sc in k for k in keys), f"{key}: shortcut {sc} not in registry"


def test_shortcut_table_in_user_guide_is_generated() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "generate_help_reference", REPO / "scripts" / "generate_help_reference.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    current = (REPO / "docs" / "user-guide.md").read_text(encoding="utf-8")
    assert module.merged_shortcuts_doc(current) == current, (
        "docs/user-guide.md shortcut table is stale; run scripts/generate_help_reference.py"
    )
    stamp = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    assert len(stamp["entries"]) == len(load_registry())


# ------------------------------------------------------------- markdown


def test_markdown_renders_headings_lists_code_tables_and_local_links() -> None:
    source = """# Title `code`

Intro with **bold**, *italic*, `inline` and a [link](exporting-datasets.md#profiles)
and an [external](https://example.org/x).

## Section one

- first
- second `x`
  continued

1. one
2. two

| A | B |
| --- | --- |
| 1 | `2` |

```text
raw <b>not html</b>
```

> quoted

## Section one
"""
    rendered = render_markdown(source)
    assert rendered.title == "Title code"
    assert '<h1 id="title-code">' in rendered.html
    assert [h.anchor for h in rendered.headings] == ["title-code", "section-one", "section-one-1"]
    assert '<a href="/help/exporting-datasets#profiles">link</a>' in rendered.html
    assert 'href="https://example.org/x" rel="noopener" target="_blank"' in rendered.html
    assert "<strong>bold</strong>" in rendered.html and "<em>italic</em>" in rendered.html
    assert "<ul><li>first</li><li>second <code>x</code> continued</li></ul>" in rendered.html
    assert "<ol><li>one</li><li>two</li></ol>" in rendered.html
    assert "<table>" in rendered.html and "<td><code>2</code></td>" in rendered.html
    assert "<pre><code>raw &lt;b&gt;not html&lt;/b&gt;</code></pre>" in rendered.html
    assert "<blockquote><p>quoted</p></blockquote>" in rendered.html


def test_markdown_escapes_html_and_rejects_script_links() -> None:
    rendered = render_markdown("<script>alert(1)</script> [x](javascript:alert(1))")
    assert "<script>" not in rendered.html
    assert 'href="#"' in rendered.html


def test_slugify_and_link_rewrite() -> None:
    assert slugify("Subtract by Order") == "subtract-by-order"
    assert slugify("5A. Brightness & contrast (view-only)") == "5a-brightness--contrast-view-only"
    assert rewrite_link("docs/user-guide.md#validate") == "/help/user-guide#validate"
    assert rewrite_link("./known-limitations.md") == "/help/known-limitations"
    assert rewrite_link("https://x.y/z.md") == "https://x.y/z.md"


def test_every_doc_renders_without_error() -> None:
    for path in sorted((REPO / "docs").glob("*.md")):
        rendered = render_markdown(path.read_text(encoding="utf-8"))
        assert rendered.html
        assert not re.search(r"<li>\s*</li>", rendered.html), path
