#!/usr/bin/env bash
# status_nightly.sh — report whether the scheduled mbirtorch nightly will actually run.
# Two things must both be true:
#   1. the SCHEDULE is installed     (macOS: the com.mbirtorch.regression agent; cluster: the
#                                     mbirtorch-nightly scrontab block)
#   2. the ENABLED kill-switch is 1   (regression.env)
# Read-only: touches no config, schedule, or results.
set -euo pipefail
if [ -t 0 ]; then
  trap '_ec=$?; [ "$_ec" -ne 0 ] && { echo; echo ">>> $(basename "$0") exited with status $_ec — press Enter to close."; read -r _ || true; }' EXIT
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/regression.env"

# recent_runs.py needs a PyYAML-capable interpreter.  Try, in order: an explicit override, the
# active env when it is the mbirtorch one, then each conda root's mbirtorch and regression envs.
_find_python() {
  local cands=() py base r roots=("$HOME/miniforge3" "$HOME/miniconda3" "$HOME/anaconda3" "$HOME/mambaforge")
  [ -n "${MBIRTORCH_STATUS_PYTHON:-}" ] && cands+=("$MBIRTORCH_STATUS_PYTHON")
  [ "${CONDA_DEFAULT_ENV:-}" = "mbirtorch" ] && cands+=("python")
  command -v conda >/dev/null 2>&1 && base="$(conda info --base 2>/dev/null || true)" || base=""
  for r in ${base:+"$base"} "${roots[@]}"; do
    cands+=("$r/envs/mbirtorch/bin/python" "$r/envs/${CONDA_ENV:-mbirtorch_regression}/bin/python")
  done
  cands+=("python3" "python")
  for py in "${cands[@]}"; do
    [ -n "$py" ] || continue
    if command -v "$py" >/dev/null 2>&1 && "$py" -c "import yaml" >/dev/null 2>&1; then
      printf '%s\n' "$py"; return 0
    fi
  done
  return 1
}

echo "mbirtorch nightly status"
scheduled=0

if [ "$(uname -s)" != "Darwin" ]; then
  # ── Linux / cluster (SLURM scrontab) ────────────────────────────────────────────────────────
  echo "  platform: cluster (SLURM scrontab)"
  if ! command -v scrontab >/dev/null 2>&1; then
    echo "  schedule: scrontab NOT FOUND — this cluster's Slurm lacks the cron feature."
  else
    B="# mbirtorch-nightly-BEGIN"; E="# mbirtorch-nightly-END"
    CUR="$(scrontab -l 2>/dev/null)" || CUR=""
    if printf '%s\n' "$CUR" | grep -qF "$B"; then
      scheduled=1
      SCHED_LINE="$(printf '%s\n' "$CUR" | sed -n "/$B/,/$E/p" | grep -vE '^#' | head -1)"
      echo "  schedule: INSTALLED — cron \"${SCHED_LINE%% bash *}\""
      echo "  wrapper:  $HERE/run_regression.sh"
    else
      echo "  schedule: not installed  (run ./enable_nightly.sh to install)"
    fi
    # The mbirjax nightly, if it is installed on this node, keeps its own block in the same
    # scrontab.  Surface it so one status call shows that both schedules are present.
    if printf '%s\n' "$CUR" | grep -qF "# mbirjax-nightly-BEGIN"; then
      echo "  (an mbirjax-nightly block is also present — the two schedules coexist in one scrontab)"
    fi
    # The weekly cluster probe keeps its own managed block (enable_probe.sh).
    if printf '%s\n' "$CUR" | grep -qF "# mbirtorch-probe-BEGIN"; then
      PL="$(printf '%s\n' "$CUR" | sed -n '/# mbirtorch-probe-BEGIN/,/# mbirtorch-probe-END/p' | grep -vE '^#' | head -1)"
      echo "  probe:    INSTALLED — cron \"${PL%% bash *}\"  (weekly cluster probe, cluster_probe.sh)"
    else
      echo "  probe:    not installed  (run ./enable_probe.sh)"
    fi
    # Slurm comments out a scron entry that was cancelled or failed to submit; nothing else says so.
    if printf '%s\n' "$CUR" | grep -q '^#DISABLED'; then
      echo "  WARNING:  scrontab has #DISABLED: entries — a cancelled or failed scron job.  Re-run the"
      echo "            matching enable script to restore it."
    fi
    if command -v squeue >/dev/null 2>&1; then
      Q="$(squeue --me --name=mbirtorch-nightly -h 2>/dev/null)" || Q=""
      [ -n "$Q" ] && { echo "  in queue now:"; printf '%s\n' "$Q" | sed 's/^/    /'; }
    fi
  fi
else
  # ── macOS / launchd ─────────────────────────────────────────────────────────────────────────
  echo "  platform: macOS (launchd)"
  LABEL="com.mbirtorch.regression"; PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  LOGDIR="$HOME/.mbirtorch/regression"
  if launchctl list 2>/dev/null | grep -qF "$LABEL"; then
    scheduled=1; echo "  schedule: LOADED ($LABEL)"
  elif [ -f "$PLIST" ]; then
    echo "  schedule: plist present but NOT loaded  (run ./enable_nightly.sh)"
  else
    echo "  schedule: not installed  (run ./enable_nightly.sh to install)"
  fi
  if [ -f "$PLIST" ]; then
    HR="$(grep -oE '<key>Hour</key><integer>[0-9]+' "$PLIST" | grep -oE '[0-9]+$' || true)"
    MN="$(grep -oE '<key>Minute</key><integer>[0-9]+' "$PLIST" | grep -oE '[0-9]+$' || true)"
    [ -n "${HR:-}" ] && printf '  runs at: daily %02d:%02d (local)\n' "$HR" "${MN:-0}"
  fi
  if [ -f "$LOGDIR/launchd.out.log" ]; then
    echo "  last out: $LOGDIR/launchd.out.log ($(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$LOGDIR/launchd.out.log"))"
  fi
fi

# ── kill-switch + overall verdict ───────────────────────────────────────────────────────────────
echo "  ENABLED kill-switch (regression.env): ${ENABLED:-0}"
echo
if [ "$scheduled" = "1" ] && [ "${ENABLED:-0}" = "1" ]; then
  # The cadence comes from a different knob per platform: the cluster reads POLL_SCHEDULE
  # (a cron expression), macOS reads MACOS_NIGHTLY_TIME (launchd ignores POLL_SCHEDULE).
  if [ "$(uname -s)" = "Darwin" ]; then _when="MACOS_NIGHTLY_TIME=\"${MACOS_NIGHTLY_TIME:-10:00}\" daily"
  else _when="POLL_SCHEDULE=\"$POLL_SCHEDULE\""; fi
  echo "✅ mbirtorch nightly WILL run — scheduled and ENABLED=1.  Wakes on $_when;"
  echo "   actual work happens only when a tracked mbirtorch branch has moved (fire-on-change)."
elif [ "$scheduled" = "1" ]; then
  echo "⏸  Scheduled, but ENABLED=0 — the wrapper exits immediately.  Set ENABLED=1 in"
  echo "   regression.env to resume (no reinstall needed)."
elif [ "${ENABLED:-0}" = "1" ]; then
  echo "❌ NOT scheduled — nothing fires.  Run ./enable_nightly.sh to install the schedule"
  echo "   (ENABLED=1 already, so it runs as soon as it's scheduled)."
else
  echo "❌ Fully off — not scheduled AND ENABLED=0.  Run ./enable_nightly.sh and set ENABLED=1."
fi

# ── recent activity ─────────────────────────────────────────────────────────────────────────────
echo
if [ "$(uname -s)" = "Darwin" ]; then
  FIRED_LOG="$HOME/.mbirtorch/regression/launchd.out.log"
else
  FIRED_LOG="$(ls -t "$WORK_DIR"/nightly-*.log 2>/dev/null | head -1 || true)"
fi
if [ -n "${FIRED_LOG:-}" ] && [ -f "$FIRED_LOG" ]; then
  if [ "$(uname -s)" = "Darwin" ]; then WHEN="$(stat -f '%Sm' -t '%Y-%m-%d %H:%M' "$FIRED_LOG")"
  else WHEN="$(date -r "$FIRED_LOG" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"; fi
  echo "last wake: $WHEN   (log: $FIRED_LOG)"
else
  echo "last wake: no torch nightly log found yet (it hasn't fired on this machine, or logs live elsewhere)"
fi

# The dependency-watch watchdog's last verdict, read out of that same log (the wrapper writes it;
# see the watchdog block in run_regression.sh).  This is the one routine
# surface that distinguishes "the GitHub Actions dependency watch found nothing" from "the watch
# stopped running" — from the outside those look identical, which is why the verdict is printed
# here.  The verdict line is the LAST of the watchdog's four, so tail -1 is what to read; the
# fallback catches a run that logged the facts but died before the verdict, and the cluster-only
# skip line a Mac log carries instead.
if [ -n "${FIRED_LOG:-}" ] && [ -f "$FIRED_LOG" ]; then
  WDV="$(grep -a 'watchdog: VERDICT' "$FIRED_LOG" 2>/dev/null | tail -1 || true)"
  [ -n "$WDV" ] || WDV="$(grep -a 'watchdog: ' "$FIRED_LOG" 2>/dev/null | tail -1 || true)"
  if [ -n "$WDV" ]; then
    echo "watchdog:  ${WDV#*] watchdog: }"   # drop the log's "[timestamp] watchdog: " prefix
  else
    echo "watchdog:  no watchdog line in that log — it runs on the cluster (gpu) nightly"
    echo "           only, and REG_NO_WATCHDOG=1 silences it."
  fi
fi

# The weekly cluster probe's last verdict (cluster only).  A status older than eight days means the
# probe itself stopped firing — its mail is the primary signal, this is the backstop.
if [ "$(uname -s)" != "Darwin" ]; then
  PSD="${PROBE_STATUS_DIR:-/depot/bouman/data/cluster_status}"; PST="$PSD/probe_status.txt"
  [ -f "$PST" ] || PST="$HOME/.mbirtorch/probe/probe_status.txt"
  if [ -f "$PST" ]; then
    _age_d=$(( ( $(date +%s) - $(stat -c %Y "$PST") ) / 86400 ))
    echo "probe:     $(cat "$PST")"
    [ "$_age_d" -le 8 ] || echo "           WARNING: that status is ${_age_d} days old — the weekly probe may be dead (scrontab -l; ./enable_probe.sh)"
  else
    echo "probe:     no status file yet ($PSD/probe_status.txt) — the weekly probe has not run"
  fi
fi

# Tile-style summary of recent runs via the dashboard's collect_data().  Prefer the nightly's
# persistent metrics clone when it has results; else this checkout.  The clone is used only when
# its origin is THIS repository: the same directory served the mbirtorch nightly while that
# nightly still lived in mbirjax_metrics, so a clone left there can hold another repository's runs.
MC_ROOT="$WORK_DIR/metrics"
REPO_ROOT_DIR="$(cd "$HERE/../.." && pwd)"
MC_ORIGIN="$(git -C "$MC_ROOT" remote get-url origin 2>/dev/null || true)"
if [ "${MC_ORIGIN%.git}" = "${METRICS_URL%.git}" ] && \
   [ -d "$MC_ROOT/results" ] && ls "$MC_ROOT"/results/*/*/regression_*.yaml >/dev/null 2>&1; then
  TARGET_ROOT="$MC_ROOT"
else
  TARGET_ROOT="$REPO_ROOT_DIR"
  [ -n "$MC_ORIGIN" ] && [ "${MC_ORIGIN%.git}" != "${METRICS_URL%.git}" ] && \
    echo "note: the clone at $MC_ROOT belongs to ${MC_ORIGIN} — reading this checkout instead."
fi
echo
PYBIN="$(_find_python || true)"
if [ -n "$PYBIN" ] && "$PYBIN" "$HERE/recent_runs.py" "$TARGET_ROOT" 6; then
  :
else
  echo "recent runs (from $TARGET_ROOT/results) — no PyYAML-capable Python found; filenames only:"
  ls -t "$TARGET_ROOT"/results/*/*/regression_*.yaml 2>/dev/null | head -6 | sed 's/^/  /' || true
fi
