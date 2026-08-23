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

reg_install_lib() {   # $1=clone dir ; caller logs/redirects/handles the exit code
  local wt="$1" plat idx
  plat="$(reg_plat)"
  if [ "$plat" = "gpu" ]; then idx="${TORCH_INDEX_URL_gpu:-}"; else idx="${TORCH_INDEX_URL_cpu:-}"; fi
  if [ -n "$idx" ]; then
    # Pre-install torch from the pinned index so the editable resolve below finds it satisfied.
    pip install --index-url "$idx" "torch>=2.13" || return 1
  fi
  pip install -e "$wt[${INSTALL_EXTRAS:-test}]"
}
