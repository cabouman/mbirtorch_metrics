#!/usr/bin/env bash
# enable_nightly.sh — install + start the scheduled MBIRTORCH nightly regression.
# It manages ONLY the mbirtorch-nightly block and agent, so running it cannot touch another
# nightly's schedule on the same machine.  Platform-aware:
#   macOS         -> launchd agent (filled from com.mbirtorch.regression.plist).
#   Linux/cluster -> a managed scrontab block (SLURM opts from run_configs.env, ONE GPU)
#                    running run_regression.sh.  Re-run after editing the knobs to apply.
set -euo pipefail
if [ -t 0 ]; then
  trap '_ec=$?; [ "$_ec" -ne 0 ] && { echo; echo ">>> $(basename "$0") exited with status $_ec — press Enter to close."; read -r _ || true; }' EXIT
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/regression.env"

if [ "$(uname -s)" != "Darwin" ]; then
  # ── Linux / cluster (SLURM scrontab) ────────────────────────────────────────────────────────
  command -v scrontab >/dev/null 2>&1 || {
    echo "ERROR: scrontab not found — this cluster's Slurm lacks the cron feature."; exit 1; }
  WRAPPER="$HERE/run_regression.sh"
  [ -f "$WRAPPER" ] || { echo "ERROR: wrapper not found at $WRAPPER"; exit 1; }
  LOGDIR="$WORK_DIR"; mkdir -p "$LOGDIR"
  B="# mbirtorch-nightly-BEGIN"; E="# mbirtorch-nightly-END"   # markers for the managed block
  OPTS="-A ${SLURM_ACCOUNT} -p ${SLURM_PARTITION} -q ${SLURM_QOS} -N1"
  OPTS="$OPTS --gpus-per-node=${SLURM_GPUS_PER_NODE} -n ${SLURM_NTASKS} -t ${SLURM_WALLTIME}"
  OPTS="$OPTS -J mbirtorch-nightly --mail-user=${NOTIFY} --mail-type=FAIL -o ${LOGDIR}/nightly-%j.log"
  BLOCK="$(printf '%s\n#SCRON %s\n%s bash %s\n%s' "$B" "$OPTS" "$POLL_SCHEDULE" "$WRAPPER" "$E")"
  # Drop any existing mbirtorch block, then append the fresh one — the sed range matches ONLY the
  # mbirtorch markers, so any other nightly's block and any other entries pass through untouched.
  CUR="$(scrontab -l 2>/dev/null | sed "/$B/,/$E/d")" || CUR=""
  { [ -n "$CUR" ] && printf '%s\n' "$CUR"; printf '%s\n' "$BLOCK"; } | scrontab -
  echo "Installed scrontab mbirtorch nightly:"
  echo "  schedule: $POLL_SCHEDULE   account: $SLURM_ACCOUNT   $SLURM_PARTITION/$SLURM_QOS   ${SLURM_GPUS_PER_NODE} GPU   t=$SLURM_WALLTIME"
  echo "  wrapper:  $WRAPPER"
  echo "  logs:     $LOGDIR/nightly-<jobid>.log"
  echo "  inspect:  scrontab -l   |   squeue --me"
  echo "  (ENABLED=$ENABLED in regression.env is the in-wrapper kill-switch; this controls the schedule.)"
  exit 0
fi

# ── macOS / launchd (the cpu series) ────────────────────────────────────────────────────
LABEL="com.mbirtorch.regression"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
TEMPLATE="$HERE/com.mbirtorch.regression.plist"
LOGDIR="$HOME/.mbirtorch/regression"

# The agent runs the ENTRY CLONE's wrapper, never this checkout's — a launchd bash cannot read
# anything under ~/Documents (macOS TCC), and the failure is silent.  See lib_mac_entry.sh.
# This clone belongs to this nightly, so refreshing it cannot perturb another one's entrypoint.
ENTRY_DIR="$HOME/.mbirtorch/entry"
# shellcheck disable=SC1091
source "$HERE/lib_mac_entry.sh"
reg_entry_clone "$ENTRY_DIR" || { echo "ERROR: could not prepare the entry clone at $ENTRY_DIR"; exit 1; }
WRAPPER="$ENTRY_DIR/tooling/regression/run_regression.sh"

[ -f "$WRAPPER" ] || { echo "ERROR: wrapper not found at $WRAPPER"; exit 1; }
command -v conda >/dev/null 2>&1 || { echo "ERROR: conda not on PATH (run from a shell where conda works)."; exit 1; }
CONDA_BIN="$(dirname "$(command -v conda)")"

RUN_TIME="${MACOS_NIGHTLY_TIME:-10:00}"
case "$RUN_TIME" in
  [0-9]:[0-5][0-9]|[0-1][0-9]:[0-5][0-9]|2[0-3]:[0-5][0-9]) ;;
  *) echo "ERROR: MACOS_NIGHTLY_TIME='$RUN_TIME' must be 24-hour HH:MM (e.g. 10:00)." >&2; exit 2 ;;
esac
HR=$((10#${RUN_TIME%%:*})); MIN=$((10#${RUN_TIME##*:}))

mkdir -p "$LOGDIR" "$HOME/Library/LaunchAgents"
sed -e "s|@LABEL@|$LABEL|g" \
    -e "s|@WRAPPER@|$WRAPPER|g" \
    -e "s|@HOUR@|$HR|g" -e "s|@MINUTE@|$MIN|g" \
    -e "s|@PATH@|$CONDA_BIN:/usr/bin:/bin:/usr/sbin:/sbin|g" \
    -e "s|@LOGOUT@|$LOGDIR/launchd.out.log|g" \
    -e "s|@LOGERR@|$LOGDIR/launchd.err.log|g" \
    "$TEMPLATE" > "$PLIST"

launchctl unload -w "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
printf 'Loaded %s — runs daily at %02d:%02d\n' "$LABEL" "$HR" "$MIN"
echo "  wrapper: $WRAPPER"
echo "           (the launchd ENTRY CLONE, refreshed just now — this checkout is not used by"
echo "            the agent, because launchd cannot read ~/Documents; see lib_mac_entry.sh)"
echo "  logs:    $LOGDIR/launchd.{out,err}.log"
echo "  (ENABLED=$ENABLED in regression.env is the in-wrapper kill-switch; this controls the schedule.)"
