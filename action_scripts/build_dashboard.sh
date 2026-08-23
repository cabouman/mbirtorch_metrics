#!/usr/bin/env bash
# Build the static performance dashboard from the YAML time series in this repo.
#
#     action_scripts/build_dashboard.sh          # or:  source action_scripts/build_dashboard.sh
#
# Thin wrapper around tooling/dashboard/build_dashboard.py (writes dashboard/index.html).
# It first tries to activate the `mbirtorch` conda env (the only dependency, PyYAML,
# lives there); if that env is already active or conda isn't found it just proceeds.
#
# Safe whether run with `bash`/`./` or `source`d: the work runs in a subshell so
# `set -e`/traps stay isolated; a nonzero exit pauses (keeping the terminal open)
# instead of closing it; a sourced run `return`s rather than exiting your shell.
# Override the interpreter with PYTHON=... .

# Were we sourced?  (`return` only works from a sourced/function context.)
if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi

(
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then
          printf "\nbuild_dashboard.sh failed (exit %s).\n" "$rc" >&2
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
  "$PY" "$REPO/tooling/dashboard/build_dashboard.py" "$@"
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
