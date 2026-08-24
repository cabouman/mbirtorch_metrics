#!/usr/bin/env bash
# Add one performance run to the tracked time series in this metrics repo, measured from a SPECIFIC
# mbirtorch commit.  Use it to seed an older commit onto the dashboard timeline.
#
# Usage:
#   action_scripts/add_run.sh --local      Measure the branch currently checked out in your CWD's
#                                           mbirtorch repo (must have NO uncommitted changes).
#   action_scripts/add_run.sh <ref>        Measure <ref> (a branch, tag, or commit sha) resolved in
#                                           the mbirtorch repo at MBIRTORCH_REPO (default: ../mbirtorch).
#   action_scripts/add_run.sh              Print this help and exit.
# Add --sbatch to any of the above (on a SLURM cluster) to SUBMIT the run as a batch job on a GPU node
# (resources from run_configs.env's SLURM_* knobs) instead of running it in this session.
#
# Either way it checks out the chosen commit into a throwaway git worktree, so your working tree is
# untouched, and measures it through the SAME pipeline as the nightly.  That means the dedicated
# `mbirtorch_regression` conda env with the worktree pip-installed editable, never your development
# env.  It writes results/<plat>/<branch>/regression_<plat>_<commit-time>_<sha8>.yaml, so the run
# lands on the dashboard timeline at its COMMIT time, comparable to the nightly runs around it.
# No gate is applied, because a backfilled run is reference data rather than a pass/fail checkpoint.
# A nonzero exit keeps the terminal open.
#
# Installing the worktree editable is what SELECTS the code under measurement.  A modern editable
# install registers a sys.meta_path finder that takes precedence over PYTHONPATH, so pointing the
# engine at the worktree via PYTHONPATH alone would NOT override a different mbirtorch already
# installed in the env; it would silently measure that one.  Hence the dedicated env and the
# `pip install -e` of the worktree (see lib_env.sh).

if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi

# --sbatch (cluster): resubmit this exact invocation, minus the flag, as a SLURM batch job and exit,
# so the measurement runs on a GPU compute node instead of here.  See tooling/regression/sbatch_submit.sh.
case " $* " in *" --sbatch "*)
  _HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; _REPO="$(cd "$_HERE/.." && pwd)"
  # shellcheck disable=SC1091
  source "$_REPO/tooling/regression/regression.env"
  # shellcheck disable=SC1091
  source "$_REPO/tooling/regression/sbatch_submit.sh"
  _ARGS=(); for _a in "$@"; do [ "$_a" = "--sbatch" ] || _ARGS+=("$_a"); done
  submit_sbatch "mbirtorch-addrun" bash "$_HERE/add_run.sh" ${_ARGS[@]+"${_ARGS[@]}"}
  _rc=$?
  if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
  ;;
esac

(
  WT=""; SRC=""
  trap 'rc=$?
        [ -n "$WT" ] && { git -C "$SRC" worktree remove --force "$WT" 2>/dev/null; rm -rf "$(dirname "$WT")" 2>/dev/null; }
        if [ "$rc" -ne 0 ]; then
          printf "\nadd_run.sh failed (exit %s).\n" "$rc" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        fi' EXIT
  set -euo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO="$(cd "$HERE/.." && pwd)"

  # Config plus the shared env and install mechanism (CONDA_ENV, INSTALL_EXTRAS, HARNESS_DEPS,
  # CONDA_PYTHON, TORCH_INDEX_URL_*).  These are the same files the nightly sources, so this stays
  # in lockstep with it.  The nightly sources them under `set -uo` with no -e, so relax -e here too:
  # a benign nonzero in the config must not abort the run.  Unset variables still trip `set -u` at
  # use, as intended.
  set +e
  # shellcheck disable=SC1091
  source "$REPO/tooling/regression/regression.env"
  # shellcheck disable=SC1091
  source "$REPO/tooling/regression/lib_env.sh"
  set -e

  usage() {
    cat <<'EOF'
Add one performance run to this metrics repo, measured from a SPECIFIC mbirtorch commit
(for example, to seed an older commit onto the dashboard timeline).

Usage:
  action_scripts/add_run.sh --local    Measure the branch checked out in your current mbirtorch repo
                                        (must have no uncommitted changes to tracked files).
  action_scripts/add_run.sh <ref>      Measure <ref> (a branch, tag, or commit sha) from the mbirtorch
                                        repo at MBIRTORCH_REPO (default: ../mbirtorch).  <ref> also
                                        names the dashboard branch group, so prefer a branch or tag.
  action_scripts/add_run.sh            Print this help and exit.

Add --sbatch (on a SLURM cluster) to submit the measurement as a batch job on a GPU node, with
resources from run_configs.env's SLURM_* knobs, instead of running it in this session.

It checks out the commit into a throwaway worktree, so your working tree is untouched, and measures
it through the same pipeline as the nightly: the dedicated mbirtorch_regression conda env with the
worktree pip-installed editable.  Your development env is untouched.  It writes
results/<plat>/<branch>/regression_<plat>_<commit-time>_<sha8>.yaml at its COMMIT time on the
timeline.  No gate is applied, because a backfilled run is not a pass/fail checkpoint.
EOF
  }
  if [ "$#" -eq 0 ]; then usage; exit 0; fi

  # ---- resolve the mbirtorch repo + the commit to measure ----------------------------------------
  if [ "$1" = "--local" ]; then
    SRC="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || true)"
    [ -n "$SRC" ] && [ -d "$SRC/mbirtorch" ] || { echo "--local: run this from inside an mbirtorch checkout." >&2; exit 2; }
    # Uncommitted CHANGES to tracked files.  Untracked files do not affect the commit we check out,
    # so they do not block.
    [ -z "$(git -C "$SRC" status --porcelain --untracked-files=no)" ] || { echo "--local: working tree has uncommitted changes — commit or stash first." >&2; exit 2; }
    COMMITISH="$(git -C "$SRC" rev-parse HEAD)"
    BRANCH="$(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
  else
    SRC="${MBIRTORCH_REPO:-"$(cd "$REPO/.." && pwd)/mbirtorch"}"
    [ -d "$SRC/.git" ] && [ -d "$SRC/mbirtorch" ] || { echo "mbirtorch repo not found at $SRC (set MBIRTORCH_REPO)." >&2; exit 2; }
    git -C "$SRC" rev-parse --verify --quiet "$1^{commit}" >/dev/null || { echo "ref '$1' not found in $SRC." >&2; exit 2; }
    COMMITISH="$1"
    BRANCH="$1"        # the ref string names the dashboard branch group
  fi
  SLUG="${BRANCH//\//_}"

  # ---- dedicated env (create if missing) + activate + harness deps (shared with the nightly) ------
  # Runs in mbirtorch_regression, never your development env, whose editable install is left alone.
  # In a `source add_run.sh` invocation this activate happens in add_run's subshell, so your current
  # shell's active env is unaffected too.
  reg_activate_env || exit $?

  # ---- platform + output dir (the same declaration the nightly makes) ----------------------------
  PLAT="$(reg_plat)"
  OUT="$REPO/results/$PLAT/$SLUG"; mkdir -p "$OUT"

  # ---- isolated checkout of the chosen commit, then measure --------------------------------------
  WT="$(mktemp -d)/lib"
  git -C "$SRC" worktree add --quiet --detach "$WT" "$COMMITISH"
  SHA="$(git -C "$WT" rev-parse --short=8 HEAD)"
  echo "add_run: $PLAT · branch=$BRANCH · commit=$SHA · src=$SRC · env=$CONDA_ENV"
  echo "         -> $OUT"

  # Install the worktree editable into the dedicated env.  THIS selects the code under measurement,
  # by re-pointing the editable finder at $WT.  The first run pulls torch, so it can be slow.
  echo "add_run: installing mbirtorch [$INSTALL_EXTRAS] into $CONDA_ENV (the first run pulls torch — can be slow)..."
  reg_install_lib "$WT" || { echo "add_run: pip install -e '$WT[$INSTALL_EXTRAS]' into $CONDA_ENV failed." >&2; exit 2; }

  # REG_GATE=0: a backfilled run is reference data, not a pass/fail checkpoint, so no nonzero exit
  # and no gate.  The engine still records a day-over-day note against the prior commit's run, if
  # there is one.  REG_LIB_ROOT gives the engine the worktree for provenance and PYTHONPATH; the
  # editable install above is what actually fixes which code imports.  REG_SKIP_TESTS is unset, so
  # the suite runs and its output lands beside the measurement, as on a nightly.
  REG_LIB_ROOT="$WT" REG_OUT_DIR="$OUT" REG_RUN_TAG="$BRANCH" REG_PLATFORM="$PLAT" REG_GATE=0 \
    REG_DEVICE_COUNTS="${DEVICE_COUNTS:-1}" REG_MEM_GATE_WINDOW="${MEM_GATE_WINDOW:-}" \
    python "$REPO/tooling/scaling_tests/performance_tracking.py"
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
