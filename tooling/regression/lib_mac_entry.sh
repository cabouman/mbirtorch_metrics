#!/usr/bin/env bash
# lib_mac_entry.sh — the macOS launchd ENTRYPOINT clone, used by enable_nightly.sh.
# macOS only; the cluster path never reaches this file.
#
# WHY THIS EXISTS.  macOS TCC protects ~/Documents (and ~/Desktop, ~/Downloads).  A launchd
# agent runs as a bare /bin/bash holding no Full Disk Access, so it cannot even READ a wrapper
# stored under a protected folder.  The run dies before its first line with
#
#     /bin/bash: .../tooling/regression/run_regression.sh: Operation not permitted
#
# and exit 126.  That is a SILENT schedule failure: `launchctl list` still shows the agent,
# `status_nightly.sh` still reports the schedule installed, and only the launchd err log says
# otherwise.  Diagnosed 2026-08-08 in the mbirjax nightly, after it had cost that repository's
# CPU series 51 consecutive nights.  The tell was that every MANUAL run worked, because Terminal
# has Full Disk Access, while no scheduled run ever did.
#
# THE FIX.  Give launchd an entrypoint OUTSIDE the protected tree: a small clone of the metrics
# repo whose only job is to be executed by the agent.  Phase 1 of each wrapper then updates its
# own $WORK_DIR/metrics clone and re-execs THAT, exactly as before — so the entry clone supplies
# only the ~46 lines of phase 1, and the measured code still comes from origin every night.
#
# WHY NOT POINT THE AGENT AT $WORK_DIR/metrics.  Phase 1 updates that clone and then re-execs
# it.  bash reads a script incrementally rather than all at once, so a script that rewrites
# itself mid-run is a real hazard.  The cluster keeps its standing checkout separate for the
# same reason.
#
# WHY A CLONE OF THIS REPOSITORY ALONE.  A refresh (`reset --hard`) rewrites files in place, so an
# entry clone belongs to one nightly.  Another nightly on the same Mac keeps its own.
#
# Requires: METRICS_URL (from the caller's regression.env / regression.env).
# Provides:  reg_entry_clone <dir>   ensure <dir> is a current shallow clone of METRICS_URL.

_regm_log() { if declare -F log >/dev/null 2>&1; then log "$@"; else echo "$*"; fi; }

reg_entry_clone() {   # $1 = entry clone directory
  local dir="$1"
  [ -n "${METRICS_URL:-}" ] || { _regm_log "ERROR: METRICS_URL is not set."; return 2; }
  # A clone of a DIFFERENT repository must never be refreshed in place.  This directory held the
  # mbirtorch entry clone while that nightly still lived in mbirjax_metrics, so a clone left here
  # can point at the wrong origin.  Refreshing it would leave the agent pointing at another
  # repository's wrapper of the same name.  Replace it instead.
  if [ -d "$dir/.git" ]; then
    local origin
    origin="$(git -C "$dir" remote get-url origin 2>/dev/null || true)"
    if [ "${origin%.git}" != "${METRICS_URL%.git}" ]; then
      _regm_log "  entry clone at $dir points at '${origin:-<none>}', not '$METRICS_URL' — re-cloning."
      rm -rf "$dir"
    fi
  fi
  if [ -d "$dir/.git" ]; then
    # Refresh to origin's tip.  Shallow fetch keeps it small; reset --hard discards any local
    # edit, which is correct for a clone nobody should be editing.  A refresh failure is NOT
    # fatal: a stale entrypoint still boots phase 1, which is the part that matters.
    if git -C "$dir" fetch -q --depth 1 origin main 2>/dev/null &&
       git -C "$dir" reset -q --hard origin/main 2>/dev/null; then
      :
    else
      _regm_log "  WARN: could not refresh the entry clone at $dir — leaving the existing copy."
    fi
  else
    mkdir -p "$(dirname "$dir")" || return 2
    git clone -q --depth 1 "$METRICS_URL" "$dir" || {
      _regm_log "ERROR: could not clone the entry copy to $dir."; return 2; }
  fi
  # The whole point is that a launchd bash can read it, so refuse a protected location.
  case "$dir" in
    "$HOME/Documents"/*|"$HOME/Desktop"/*|"$HOME/Downloads"/*)
      _regm_log "ERROR: entry clone at $dir is inside a TCC-protected folder — launchd cannot read it."
      return 2 ;;
  esac
  return 0
}
