#!/usr/bin/env bash
# lib_env.sh — the "prepare the dedicated TORCH regression env + install an mbirtorch clone"
# mechanism, sourced by run_regression.sh.
#
# Why the editable install (not just PYTHONPATH) is load-bearing — the lib_env.sh lesson applies
# unchanged: a modern `pip install -e` registers a MetaPathFinder consulted BEFORE PYTHONPATH, so
# any editable mbirtorch already present in the interpreter's env would shadow a PYTHONPATH clone.
# Installing the clone editable into the DEDICATED env re-points the finder at the code under
# measurement.  (The dedicated env exists so this re-pointing never touches a dev env.)
#
# torch selects its CUDA build through the WHEEL INDEX, not a pip extra.  On gpu,
# reg_install_lib pre-installs torch from
# TORCH_INDEX_URL_gpu (cu130) so the editable install finds `torch>=2.13` already satisfied; the
# default index would otherwise serve a build whose CUDA may not match the node's module.
#
# Requires (set by sourcing regression.env first): CONDA_ENV, CONDA_PYTHON,
#   HARNESS_DEPS, INSTALL_EXTRAS, TORCH_INDEX_URL_gpu / TORCH_INDEX_URL_cpu.
# Provides:
#   reg_activate_env   REG_VENV set -> activate that existing venv (trial path);
#                            else auto-create + activate $CONDA_ENV -> install $HARNESS_DEPS
#   reg_plat           echo "gpu" | "cpu"  (nvidia-smi signal; the writer then
#                            VERIFIES this declaration against torch.cuda and aborts on mismatch)
#   reg_install_lib WT pip-install torch (pinned index, GPU only) + editable "WT[extras]"
# Logs via the caller's log() if it defines one, else a plain prefix.

_regt_log() { if declare -F log >/dev/null 2>&1; then log "$@"; else echo "[lib_torch_env] $*"; fi; }

reg_activate_env() {
  # TRIAL SEAM: an existing venv
  # substitutes for the dedicated conda env, so the pre-schedule trial does not depend on the
  # env-creation step.  Production leaves REG_VENV unset.
  if [ -n "${REG_VENV:-}" ]; then
    [ -f "$REG_VENV/bin/activate" ] || {
      _regt_log "FATAL: REG_VENV='$REG_VENV' has no bin/activate."; return 2; }
    # shellcheck disable=SC1091
    source "$REG_VENV/bin/activate"
    _regt_log "using trial venv $REG_VENV (python: $(command -v python))"
  else
    if ! command -v conda >/dev/null 2>&1; then
      for s in "$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
        [ -f "$s/etc/profile.d/conda.sh" ] && { . "$s/etc/profile.d/conda.sh"; break; }
      done
    fi
    command -v conda >/dev/null 2>&1 || {
      _regt_log "FATAL: 'conda' not found.  On the cluster, PREAMBLE_FILE must load conda."
      return 2
    }
    # shellcheck disable=SC1091
    source "$(conda info --base)/etc/profile.d/conda.sh"
    if ! conda env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
      _regt_log "conda env '$CONDA_ENV' not found — creating it (one-time on this machine)."
      conda create -y -q -n "$CONDA_ENV" "python=${CONDA_PYTHON:-3.12}" \
        || { _regt_log "FATAL: could not create conda env '$CONDA_ENV'."; return 2; }
    fi
    conda activate "$CONDA_ENV" || { _regt_log "FATAL: conda activate '$CONDA_ENV' failed."; return 2; }
  fi
  # Harness deps (scaling_common imports matplotlib/ruamel at module level) — idempotent.
  if [ -n "${HARNESS_DEPS:-}" ]; then
    # shellcheck disable=SC2086
    pip install --quiet $HARNESS_DEPS || _regt_log "WARN: harness deps install failed (engine may not import)."
  fi
}

reg_plat() {   # hardware signal only — no torch import here; the writer verifies the claim
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
    echo "gpu"
  else
    echo "cpu"
  fi
}

reg_torch_index() {   # the wheel index this platform pins torch to ("" = default PyPI)
  if [ "$(reg_plat)" = "gpu" ]; then echo "${TORCH_INDEX_URL_gpu:-}"; else echo "${TORCH_INDEX_URL_cpu:-}"; fi
}

# reg_torch_spec WT : the torch requirement from the clone's pyproject, e.g. "torch>=2.13,!=2.14.0",
# read with tomllib so exclusions count.  Falls back to torch>=2.13 with a loud log rather than
# returning garbage, which would fail every branch's install and make a silently empty night.
reg_torch_spec() {
  local wt="$1" spec
  spec="$(python - "$wt/pyproject.toml" <<'PY' 2>/dev/null
import re, sys, tomllib
try:
    d = tomllib.load(open(sys.argv[1], "rb"))
    for dep in d["project"]["dependencies"]:
        if re.match(r"^torch\s*([<>=!~;\[].*)?$", dep.strip()):
            print(dep.strip().replace(" ", "")); break
except Exception:
    pass
PY
)"
  [ -n "$spec" ] || { _regt_log "WARN: could not read the torch requirement from $wt/pyproject.toml — using torch>=2.13"; spec="torch>=2.13"; }
  printf '%s' "$spec"
}

reg_installed_torch() {   # base version of the installed torch ("2.13.0"), or "" when it does not import
  python -c 'import torch; print(torch.__version__.split("+")[0])' 2>/dev/null || true
}

# reg_index_newest_torch : the base version the pinned index would install for this Python, or ""
# (no pinned index, or the query failed).  This, not PyPI's newest, is what a canary can measure.
reg_index_newest_torch() {
  local idx; idx="$(reg_torch_index)"
  [ -n "$idx" ] || { echo ""; return 0; }
  pip index versions torch --index-url "$idx" 2>/dev/null | head -1 | sed -n 's/^torch (\([^)+]*\).*$/\1/p'
}

# reg_torch_satisfies SPEC : 0 when the installed torch satisfies SPEC; 3 when it does not; 0 when
# `packaging` is missing (the check is skipped, not failed).
reg_torch_satisfies() {
  python - "$1" <<'PY'
import sys
try:
    from packaging.requirements import Requirement
    from packaging.version import Version
    import torch
except ImportError:
    sys.exit(0)
req = Requirement(sys.argv[1]); v = Version(torch.__version__.split("+")[0])
sys.exit(0 if req.specifier.contains(v, prereleases=False) else 3)
PY
}

# reg_pin_torch VERSION : install exactly that torch from the pinned index and confirm it.  PEP 440
# matches "2.13.0" to the index's "2.13.0+cu130" (a specifier without a local label ignores it).
reg_pin_torch() {
  local idx; idx="$(reg_torch_index)"
  if [ -n "$idx" ]; then pip install --index-url "$idx" "torch==$1" || return 1
  else pip install "torch==$1" || return 1; fi
  [ "$(reg_installed_torch)" = "$1" ]
}

reg_install_lib() {   # $1=clone dir ; caller logs/redirects/handles the exit code
  local wt="$1" idx spec
  idx="$(reg_torch_index)"; spec="$(reg_torch_spec "$wt")"
  if [ -n "$idx" ]; then
    # Pre-install the clone's OWN torch requirement from the pinned index, so the editable resolve
    # below finds it satisfied.  This is what keeps an exclusion in pyproject from making pip
    # re-resolve torch from the default index and quietly replace the cu-build.
    pip install --index-url "$idx" "$spec" || return 1
  fi
  pip install -e "$wt[${INSTALL_EXTRAS:-test}]" || return 1
  reg_torch_satisfies "$spec" || { _regt_log "ERROR: installed torch '$(reg_installed_torch)' does not satisfy '$spec'."; return 1; }
}

# ── Dependency canary helpers (used only when DEP_CANARY_ENABLED=1) ───────────────────────────
# reg_upgrade_torch: force torch to the newest the pinned index serves, and nothing else.  The
# per-branch reg_install_lib that follows re-resolves against the branch's pyproject, so a version
# that pyproject excludes is pulled back down and the exclusion stays single-sourced there.
reg_upgrade_torch() {   # caller logs, redirects, and handles the exit code; fails when torch no longer imports
  local idx; idx="$(reg_torch_index)"
  if [ -n "$idx" ]; then pip install -U --index-url "$idx" torch || return 1
  else pip install -U torch || return 1; fi
  [ -n "$(reg_installed_torch)" ]
}

# reg_upgrade_all: the periodic FULL refresh.  Eager-upgrade every dependency the pyproject allows,
# not just torch, so non-torch drift (numpy, triton, a CUDA runtime wheel) is caught too.  Uses the
# editable install, so any pyproject pin is honored natively.
reg_upgrade_all() {   # $1=clone dir $2=torch version to HOLD ; caller logs, redirects, handles the exit code
  # Everything but torch is eager-upgraded: a constraints file holds torch at the ledger's version
  # (the installed +cu build already satisfies "torch==X", so pip leaves it alone), and the pinned
  # index is an extra index so a torch-ecosystem package that only lives there still resolves.
  local c idx rc; c="$(mktemp)"; idx="$(reg_torch_index)"
  printf 'torch==%s\n' "$2" > "$c"
  # shellcheck disable=SC2086
  pip install -e "$1[${INSTALL_EXTRAS:-test}]" --upgrade --upgrade-strategy eager -c "$c" ${idx:+--extra-index-url "$idx"}
  rc=$?; rm -f "$c"; return "$rc"
}
