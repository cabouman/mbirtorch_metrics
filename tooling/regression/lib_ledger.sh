#!/usr/bin/env bash
# lib_ledger.sh — the torch version ledger: one append-only text file per platform,
# state/<plat>/torch_ledger.txt in the metrics clone, written only by the nightly wrapper and read
# by the wrapper (the torch the env must run) and by mbirtorch's dependency watch (what to
# propose).  Design record: mbirtorch_plans/plans/mbirtorch_metrics/torch_version_policy_plan.md.
#     <version> <state> <date> <evidence...>       latest line per version wins
# States: candidate (installed, verdict pending) | validated | rejected | unavailable (PyPI has it,
# the pinned wheel index does not).  Pure bash; test_lib_ledger.sh exercises every function.

_ledger_lines() { [ -f "$1" ] && grep -vE '^[[:space:]]*(#|$)' "$1" || true; }

# Version sort, newest first, without `sort -V` (macOS ships a sort that may lack it): pad each
# of the first four dot-separated fields to six digits and sort that key.
_ledger_vsort_desc() {
  awk -F. '{k=""; for (i=1;i<=4;i++) k=k sprintf("%06d", (i<=NF ? $i : 0)); print k " " $0}' \
    | sort -r | cut -d' ' -f2-
}

ledger_latest_lines() {   # $1=file -> the latest line per version, in first-seen order
  _ledger_lines "$1" | awk '{ last[$1]=$0; if (!($1 in seen)) { order[++n]=$1; seen[$1]=1 } }
                             END { for (i=1;i<=n;i++) print last[order[i]] }'
}

ledger_state() {          # $1=file $2=version -> its latest state, or "" when absent
  _ledger_lines "$1" | awk -v v="$2" '$1==v {s=$2} END {print s}'
}

ledger_versions_in_state() {   # $1=file $2=state -> versions in that state, newest first
  ledger_latest_lines "$1" | awk -v s="$2" '$2==s {print $1}' | _ledger_vsort_desc
}

ledger_runs_on() { ledger_versions_in_state "$1" validated | head -1; }   # newest validated, or ""

ledger_newest() { ledger_latest_lines "$1" | awk '{print $1}' | _ledger_vsort_desc | head -1; }

ledger_append() {   # $1=file $2=version $3=state $4...=evidence
  # Idempotent per (version, state): returns 0 when a line was written, 1 when the latest line
  # for that version already carries the state (so a retried night does not repeat itself).
  local f="$1" v="$2" s="$3"; shift 3
  [ "$(ledger_state "$f" "$v")" = "$s" ] && return 1
  mkdir -p "$(dirname "$f")"
  printf '%s %s %s %s\n' "$v" "$s" "$(date +%F)" "$*" >> "$f"
}
