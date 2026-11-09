#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
# Runs lint, type checks and the CPU-only test suite. Pass --browser to add
# Playwright tests (Chromium).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
ruff check src tests scripts
mypy src
pytest -m "not browser and not real_model and not gpu"
python scripts/add_license_headers.py --check
python scripts/generate_help_reference.py --check
if [ "${1:-}" = "--browser" ]; then
  pytest -m browser --browser chromium
fi
python -m afmlabel.diagnostics.privacy
echo "All checks passed."
