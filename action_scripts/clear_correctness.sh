#!/usr/bin/env bash
# Clear (acknowledge) reviewed correctness divergences through a date — see the correctness-gating
# design note D6.  Writes results/correctness_acks.yaml `cleared_through: <date>`; every correctness
# divergence on a commit dated <= that date is then acknowledged (greyed on the dashboard, dropped
# from the banner / browser-tab badge).
#
#   action_scripts/clear_correctness.sh              # the one-liner: print status, then confirm [Y/n]
#   action_scripts/clear_correctness.sh 2026-06-20   # clear through an explicit earlier date
#   action_scripts/clear_correctness.sh --status     # print status only, never prompt or write
#
# Thin wrapper around tooling/dashboard/clear_correctness.py (reuses build_dashboard.collect_data, so it
# shows exactly what the dashboard shows).  After writing, rebuild with action_scripts/build_dashboard.sh
# and commit the acks file.  Override the interpreter with PYTHON=... .

# Were we sourced?  (`return` only works from a sourced/function context.)
if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi

(
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then
          printf "\nclear_correctness.sh failed (exit %s).\n" "$rc" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        fi' EXIT
  set -euo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  REPO="$(cd "$HERE/.." && pwd)"

  # Best-effort activation of the mbirtorch conda env (never fatal).
  if [ "${CONDA_DEFAULT_ENV:-}" != "mbirtorch" ]; then
    set +e
    command -v conda >/dev/null 2>&1 || for s in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
      [ -f "$s/etc/profile.d/conda.sh" ] && . "$s/etc/profile.d/conda.sh" && break
    done
    command -v conda >/dev/null 2>&1 && eval "$(conda shell.bash hook)" && conda activate mbirtorch
    set -e
  fi

  PY="${PYTHON:-}"
  if [ -z "$PY" ]; then
    if command -v python >/dev/null 2>&1; then PY=python
    elif command -v python3 >/dev/null 2>&1; then PY=python3
    else echo "No python interpreter found — activate the mbirtorch conda env first." >&2; exit 127; fi
  fi
  "$PY" "$REPO/tooling/dashboard/clear_correctness.py" "$@"
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
