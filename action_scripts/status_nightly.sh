#!/usr/bin/env bash
# Report whether the scheduled MBIRTORCH nightly will run (forwards to
# tooling/regression/status_nightly.sh).  Read-only: checks the torch schedule (launchd
# agent / mbirtorch-nightly scrontab block) AND the ENABLED kill-switch in regression.env,
# then prints a verdict and the recent-runs table.
# Safe whether run with bash/./ or sourced; keeps the terminal open on a nonzero exit.
if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi
(
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then
          printf "\nstatus_nightly.sh failed (exit %s).\n" "$rc" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        fi' EXIT
  set -euo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  "$HERE/../tooling/regression/status_nightly.sh" "$@"
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
