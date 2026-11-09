# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
"""Every source file carries the MIT licence header (scripts/add_license_headers.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_script():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "add_license_headers", REPO / "scripts" / "add_license_headers.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_source_file_has_the_mit_header() -> None:
    script = _load_script()
    missing = [
        str(path.relative_to(REPO))
        for path in script.iter_source_files()
        if not script.has_header(path.read_text(encoding="utf-8"))
    ]
    assert not missing, "run: python scripts/add_license_headers.py\n" + "\n".join(missing)


def test_license_file_is_mit_with_the_institutional_copyright() -> None:
    text = (REPO / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License")
    assert "Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava" in text


def test_header_is_placed_after_a_shebang_or_doctype(tmp_path: Path) -> None:
    script = _load_script()
    shell = tmp_path / "x.sh"
    shell.write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
    assert script.add_header(shell) is True
    lines = shell.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#!/usr/bin/env bash" and lines[1].startswith("# SPDX")
    assert script.add_header(shell) is False  # idempotent
    page = tmp_path / "p.html"
    page.write_text("<!doctype html>\n<html></html>\n", encoding="utf-8")
    script.add_header(page)
    lines = page.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "<!doctype html>" and lines[1].startswith("{# SPDX")
