#!/bin/bash
#SBATCH -A bouman
#SBATCH -p ai
#SBATCH -q normal
#SBATCH -N 1
#SBATCH --gpus-per-node=1
#SBATCH -n 14
#SBATCH -t 02:00:00
#SBATCH -J mbirtorch-nightly
#SBATCH --mail-user=buzzard@purdue.edu
#SBATCH --mail-type=FAIL
#SBATCH -o slurm-mbirtorch-nightly-%j.log
#
# nightly_regression.slurm — run ONE mbirtorch nightly regression pass as a batch job on
# Gautschi.  Sibling of nightly_regression.slurm; ONE GPU (the n=1 sweep needs no more).
#
# This is for a MANUAL batch test of the cluster pipeline:
#     cd <your standing mbirtorch_metrics checkout>
#     sbatch tooling/regression/nightly_regression.slurm
# The SCHEDULED nightly does NOT use this file: enable_nightly.sh installs a scrontab entry
# that runs run_regression.sh with the SLURM_* options from run_configs.env, so
# keep the #SBATCH directives above in sync with those knobs (or just edit run_configs.env
# and re-run enable_nightly.sh for the real schedule).
#
# It forwards to run_regression.sh, which sources the node preamble (module load conda +
# proxy via PREAMBLE_FILE in regression.env), updates its persistent metrics clone, and
# measures every tracked mbirtorch branch whose tip moved.
set -uo pipefail
REPO="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
exec bash "$REPO/tooling/regression/run_regression.sh"
