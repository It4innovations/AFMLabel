#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 IT4Innovations, VSB-Technical University of Ostrava
#SBATCH --job-name=afmlabel
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --output=afmlabel-%j.log
# Submit with account/partition/QOS/time/GPU on the sbatch command line, e.g.
#   sbatch --account=<acct> --partition=<part> --time=08:00:00 --gres=gpu:1 \
#          --cpus-per-task=8 --mem=32G scripts/slurm/launch_batch.sh
# Nothing cluster-specific is hard-coded here.
set -euo pipefail

AFMLABEL_ROOT="${AFMLABEL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
export AFMLABEL_ROOT
exec bash "$AFMLABEL_ROOT/scripts/slurm/_serve_on_node.sh"
