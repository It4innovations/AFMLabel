#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
# Creates the venv on Linux and installs AFMLabel with the dev extra.
# Usage: scripts/dev/bootstrap.sh [python-executable] [--with-browsers]
set -euo pipefail

PYTHON="${1:-python3.12}"
WITH_BROWSERS="${2:-}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
if [ "$WITH_BROWSERS" = "--with-browsers" ]; then
  playwright install --with-deps chromium firefox
fi
mkdir -p tmp
echo "Done. Activate with: source .venv/bin/activate"
