#!/usr/bin/env bash
# Stop the scheduled MBIRTORCH nightly (forwards to tooling/regression/disable_nightly.sh).
# Removes only the mbirtorch schedule (launchd agent / mbirtorch-nightly scrontab block); the
# any other nightly on this machine, and all config and results, are untouched.
# Safe whether run with bash/./ or sourced; keeps the terminal open on a nonzero exit.
if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi
(
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then
          printf "\ndisable_nightly.sh failed (exit %s).\n" "$rc" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        fi' EXIT
  set -euo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  "$HERE/../tooling/regression/disable_nightly.sh" "$@"
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
