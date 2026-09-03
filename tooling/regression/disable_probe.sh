#!/usr/bin/env bash
# disable_probe.sh — remove the weekly cluster probe's managed scrontab block (and with it the
# pending scron job).  Touches nothing else: not the nightly's block, not the status files.
set -euo pipefail
if [ -t 0 ]; then
  trap '_ec=$?; [ "$_ec" -ne 0 ] && { echo; echo ">>> $(basename "$0") exited with status $_ec — press Enter to close."; read -r _ || true; }' EXIT
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib_scron.sh"
[ "$(uname -s)" != "Darwin" ] || { echo "disable_probe: the probe runs on the cluster only; nothing to do on macOS."; exit 0; }
command -v scrontab >/dev/null 2>&1 || { echo "disable_probe: scrontab not found; nothing to disable."; exit 0; }
if scron_block_remove mbirtorch-probe; then echo "Removed the mbirtorch-probe scrontab block (other entries left intact)."
else echo "No mbirtorch-probe scrontab block found; nothing to disable."; fi
