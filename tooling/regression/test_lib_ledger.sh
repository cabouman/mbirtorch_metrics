#!/usr/bin/env bash
# test_lib_ledger.sh — unit checks for lib_ledger.sh on a temp file.   bash test_lib_ledger.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/lib_ledger.sh"
L="$(mktemp)"; trap 'rm -f "$L"' EXIT
fail() { echo "FAIL: $*"; exit 1; }
[ -z "$(ledger_runs_on "$L")" ] || fail "an absent ledger has no runs_on"
[ -z "$(ledger_state "$L" 2.13.0)" ] || fail "an absent ledger has no state"
ledger_append "$L" 2.13.0 validated bootstrap || fail "first append must return 0"
if ledger_append "$L" 2.13.0 validated bootstrap; then fail "a repeated (version,state) must return 1"; fi
[ "$(grep -c . "$L")" = 1 ] || fail "idempotent append wrote a line"
ledger_append "$L" 2.14.0 candidate "pypi python=3.12" || true
[ "$(ledger_state "$L" 2.14.0)" = candidate ] || fail "state candidate"
ledger_append "$L" 2.14.0 rejected "run.yaml: hard gate FAIL" || true
[ "$(ledger_state "$L" 2.14.0)" = rejected ] || fail "latest line wins"
[ "$(ledger_runs_on "$L")" = 2.13.0 ] || fail "runs_on ignores rejected"
ledger_append "$L" 2.15.0 validated run || true
ledger_append "$L" 2.9.0 validated old || true
[ "$(ledger_runs_on "$L")" = 2.15.0 ] || fail "runs_on is the newest validated by version order, got $(ledger_runs_on "$L")"
[ "$(ledger_newest "$L")" = 2.15.0 ] || fail "newest"
ledger_append "$L" 2.16.0 unavailable "index=cu130" || true
[ "$(ledger_newest "$L")" = 2.16.0 ] || fail "newest counts unavailable"
[ "$(ledger_runs_on "$L")" = 2.15.0 ] || fail "runs_on ignores unavailable"
got="$(ledger_versions_in_state "$L" validated | tr '\n' ' ')"
[ "$got" = "2.15.0 2.13.0 2.9.0 " ] || fail "validated versions newest first, got '$got'"
printf '# comment\n\n' >> "$L"
[ "$(ledger_state "$L" 2.16.0)" = unavailable ] || fail "comments and blanks are ignored"
echo "lib_ledger: all checks passed"
