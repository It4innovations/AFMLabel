#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
# Start AFMLabel inside an interactive Slurm allocation.
#
# Account, partition, QOS and resources come from environment variables
# (never hard-coded):
#   AFMLABEL_ACCOUNT, AFMLABEL_PARTITION, AFMLABEL_QOS,
#   AFMLABEL_GPUS (default 0), AFMLABEL_CPUS (default 8),
#   AFMLABEL_MEM (default 32G), AFMLABEL_TIME (default 04:00:00),
#   AFMLABEL_ROOT (repo), AFMLABEL_VENV (.venv), AFMLABEL_PORT (default 9080),
#   AFMLABEL_WORKSPACE__ROOT (workspace directory).
set -euo pipefail

AFMLABEL_ROOT="${AFMLABEL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export AFMLABEL_ROOT
SALLOC_ARGS=(--nodes=1 --ntasks=1 --cpus-per-task="${AFMLABEL_CPUS:-8}" --mem="${AFMLABEL_MEM:-32G}" --time="${AFMLABEL_TIME:-04:00:00}")
[ -n "${AFMLABEL_ACCOUNT:-}" ] && SALLOC_ARGS+=(--account="$AFMLABEL_ACCOUNT")
[ -n "${AFMLABEL_PARTITION:-}" ] && SALLOC_ARGS+=(--partition="$AFMLABEL_PARTITION")
[ -n "${AFMLABEL_QOS:-}" ] && SALLOC_ARGS+=(--qos="$AFMLABEL_QOS")
if [ "${AFMLABEL_GPUS:-0}" != "0" ]; then
  SALLOC_ARGS+=(--gres="gpu:${AFMLABEL_GPUS}")
fi

echo "requesting allocation: salloc ${SALLOC_ARGS[*]}"
exec salloc "${SALLOC_ARGS[@]}" srun --pty bash "$AFMLABEL_ROOT/scripts/slurm/_serve_on_node.sh"
