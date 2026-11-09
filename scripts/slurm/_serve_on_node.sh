#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
# Runs on the allocated compute node: activates the venv, picks a port,
# prints connection details and starts AFMLabel bound to localhost.
set -euo pipefail

AFMLABEL_ROOT="${AFMLABEL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
AFMLABEL_VENV="${AFMLABEL_VENV:-$AFMLABEL_ROOT/.venv}"
PORT="${AFMLABEL_PORT:-9080}"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1

# shellcheck disable=SC1091
source "$AFMLABEL_VENV/bin/activate"
cd "$AFMLABEL_ROOT"

# First free port starting at $PORT.
while python - "$PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
with socket.socket() as s:
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        sys.exit(0)   # busy -> loop continues
sys.exit(1)           # free -> loop stops
PY
do PORT=$((PORT + 1)); done

NODE="$(hostname)"
INFO_FILE="afmlabel-${SLURM_JOB_ID:-interactive}.node"
{
  echo "node=$NODE"
  echo "port=$PORT"
  echo "workspace=${AFMLABEL_WORKSPACE__ROOT:-./workspace}"
  echo "started=$(date -Is)"
} > "$INFO_FILE"

cat <<EOF
AFMLabel is starting on $NODE:$PORT (bound to 127.0.0.1 on the node).
Connection details written to $INFO_FILE.
SSH tunnel example (adjust login node and user):
  ssh -N -L $PORT:127.0.0.1:$PORT -J <user>@<login-node> <user>@$NODE
then open http://127.0.0.1:$PORT in your browser.
EOF

exec afmlabel serve --host 127.0.0.1 --port "$PORT"
