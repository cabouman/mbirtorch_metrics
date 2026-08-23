#!/usr/bin/env bash
# Run ONE mbirtorch nightly regression pass right now, by hand.
#
# Forwards to the real harness,
# `tooling/regression/run_regression.sh` — the exact code the scheduler will run — so this is
# a faithful single pass: for each tracked mbirtorch branch whose remote tip moved since it was
# last measured, it clones the tip, runs the suite + the torch writer, writes results into the
# harness's persistent metrics clone, and pushes to GitHub.
#
# The harness manages its own conda env (CONDA_ENV in regression.env, default
# `mbirtorch_regression`, auto-created).  Trial knobs pass straight through the environment:
# REG_SMOKE=1 (fast 1-cell plumbing check), REG_NO_PUSH=1 (write locally, push
# nothing), REG_FORCE=1 (re-measure an unmoved tip).
#
# On a SLURM cluster, add --sbatch to SUBMIT this pass as a batch job on a GPU node (resources
# from run_configs.env's SLURM_* knobs) instead of running it in this session.
#
# Exit-code meaning (faithful to the scheduler): 0 = clean; 1 = a hard-gate REGRESSION was
# detected (an ALERT, not a script failure); >=2 = the harness itself failed.

if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi

# --sbatch (cluster): resubmit this pass as a SLURM batch job (minus the flag) and exit.
case " $* " in *" --sbatch "*)
  _HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; _REPO="$(cd "$_HERE/.." && pwd)"
  # shellcheck disable=SC1091
  source "$_REPO/tooling/regression/regression.env"
  # Map the torch knobs onto the names sbatch_submit.sh reads, so the shared helper submits with
  # this nightly's own SLURM resources from run_configs.env.
  SLURM_ACCOUNT="$SLURM_ACCOUNT"; SLURM_PARTITION="$SLURM_PARTITION"
  SLURM_QOS="$SLURM_QOS"; SLURM_GPUS_PER_NODE="$SLURM_GPUS_PER_NODE"
  SLURM_NTASKS="$SLURM_NTASKS"; SLURM_WALLTIME="$SLURM_WALLTIME"
  # shellcheck disable=SC1091
  source "$_REPO/tooling/regression/sbatch_submit.sh"
  _ARGS=(); for _a in "$@"; do [ "$_a" = "--sbatch" ] || _ARGS+=("$_a"); done
  submit_sbatch "mbirtorch-night" bash "$_HERE/run_one_night.sh" "${_ARGS[@]}"
  _rc=$?
  if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
  ;;
esac

(
  trap 'rc=$?;
        if [ "$rc" -eq 1 ]; then
          printf "\nrun_one_night.sh: completed — hard-gate regression(s) DETECTED (exit 1).\n" >&2
          printf "This is an ALERT, not a failure: the run finished and results were recorded.\n" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        elif [ "$rc" -ne 0 ]; then
          printf "\nrun_one_night.sh FAILED (exit %s) — harness/setup error.\n" "$rc" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        fi' EXIT
  set -euo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # Detach the harness's stdin so its own interactive pause never installs — this wrapper owns
  # the single user-facing message + pause above.
  "$HERE/../tooling/regression/run_regression.sh" "$@" </dev/null
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
