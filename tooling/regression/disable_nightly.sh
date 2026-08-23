#!/usr/bin/env bash
# disable_nightly.sh — stop the scheduled MBIRTORCH nightly regression.  Platform-aware:
#   macOS         -> unload + remove the com.mbirtorch.regression launchd agent.
#   Linux/cluster -> remove the managed mbirtorch-nightly scrontab block (any other nightly's
#                    block and any other entries are left intact).
# Stops the SCHEDULE only; config and results are untouched.  (For a quick pause without
# uninstalling, set ENABLED=0 in regression.env.)
set -euo pipefail
if [ -t 0 ]; then
  trap '_ec=$?; [ "$_ec" -ne 0 ] && { echo; echo ">>> $(basename "$0") exited with status $_ec — press Enter to close."; read -r _ || true; }' EXIT
fi
if [ "$(uname -s)" != "Darwin" ]; then
  # ── Linux / cluster (SLURM scrontab) ────────────────────────────────────────────────────────
  command -v scrontab >/dev/null 2>&1 || { echo "disable_torch_nightly: scrontab not found; nothing to disable."; exit 0; }
  B="# mbirtorch-nightly-BEGIN"; E="# mbirtorch-nightly-END"
  CUR="$(scrontab -l 2>/dev/null)" || CUR=""
  if printf '%s\n' "$CUR" | grep -qF "$B"; then
    printf '%s\n' "$CUR" | sed "/$B/,/$E/d" | scrontab -
    echo "Removed the mbirtorch-nightly scrontab block (other entries left intact)."
  else
    echo "No mbirtorch-nightly scrontab block found; nothing to disable."
  fi
  exit 0
fi

# ── macOS / launchd ───────────────────────────────────────────────────────────────────────────
LABEL="com.mbirtorch.regression"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
launchctl unload -w "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "Disabled $LABEL (unloaded + removed $PLIST)."
