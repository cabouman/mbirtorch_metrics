#!/usr/bin/env bash
# Install + start the scheduled MBIRTORCH nightly (forwards to tooling/regression/enable_nightly.sh).
# Platform-aware: macOS uses launchd; the cluster (Gautschi) uses a managed SLURM scrontab block
# named mbirtorch-nightly, which never touches another nightly's block on the same node.
# Safe whether run with bash/./ or sourced; keeps the terminal open on a nonzero exit.
if (return 0 2>/dev/null); then _sourced=1; else _sourced=0; fi
(
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then
          printf "\nenable_nightly.sh failed (exit %s).\n" "$rc" >&2
          [ -t 0 ] && read -r -p "Press Enter to close... " _ </dev/tty || true
        fi' EXIT
  set -euo pipefail
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  "$HERE/../tooling/regression/enable_nightly.sh" "$@"
)
_rc=$?
if [ "$_sourced" -eq 1 ]; then return "$_rc"; else exit "$_rc"; fi
