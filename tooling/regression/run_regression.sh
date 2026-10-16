#!/usr/bin/env bash
# run_regression.sh — fire-on-change nightly regression driver for mbirtorch.
#
# Phase 1 (bootstrap, runs from wherever scrontab/launchd invokes it): source the node preamble
#   (proxy + modules), update (or clone) the PERSISTENT metrics clone at $WORK_DIR/metrics, and
#   re-exec ITS copy of this script — so harness/engine/wrapper updates on the remote are always used.
# Phase 2 (work, runs from the persistent metrics clone): for each tracked branch whose remote head
#   moved since last measured (git ls-remote vs metrics state/<plat>/), make a SHALLOW single-branch
#   clone of the mbirtorch tip, install it into the dedicated env, run its test suite, run the
#   engine (performance_tracking.py — gate + records + dashboard files), record results + the
#   measured SHA into the metrics clone, and push (non-fatal).
#
# Config: regression.env.  Exits non-zero ONLY on a hard-gate regression, so the slurm mail from
# the mbirtorch-nightly job is a real alert; setup and transport hiccups are WARNs.
#
# Trial knobs (production leaves all of them unset):
#   REG_SMOKE=1     a toy one-cell sweep into a temp directory, for plumbing checks
#   REG_NO_PUSH=1   write results and state locally, skip the commit and push
#   REG_FORCE=1     treat every tracked branch as changed, to re-measure an unmoved tip
#   REG_VENV=<dir>  use this existing venv instead of the dedicated conda env (see lib_env.sh)
#
# Watchdog knob (production leaves it unset):
#   REG_NO_WATCHDOG=1  skip the dependency-watch watchdog line (see its block below) — the
#                         one way to silence it without editing this file
set -uo pipefail
# Keep an INTERACTIVE terminal open on a nonzero exit so the message stays visible; a tty-less run
# (launchd/scrontab/slurm) skips the pause.  Exit 1 is a REGRESSION (an alert — the run completed),
# exit >=2 is a harness/setup error.
if [ -t 0 ]; then
  trap '_ec=$?;
    if [ "$_ec" -eq 1 ]; then
      echo; echo ">>> $(basename "$0"): regression(s) DETECTED (exit 1) — an alert, not a failure.  Press Enter to close."; read -r _ || true
    elif [ "$_ec" -ne 0 ]; then
      echo; echo ">>> $(basename "$0") exited with status $_ec (harness/setup error) — press Enter to close."; read -r _ || true
    fi' EXIT
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/regression.env"
log() { echo "[$(date '+%F %T')] $*"; }

[ "${ENABLED:-0}" = "1" ] || { log "ENABLED=0 — nothing to do."; exit 0; }

# Node preamble (cluster: `module load` conda/cuda + export HTTPS_PROXY; absent on the Mac).
# SOURCED in BOTH phases — conda's shell function does not survive the phase-1->2 exec.
if [ -n "${PREAMBLE_FILE:-}" ] && [ -f "$PREAMBLE_FILE" ]; then
  # shellcheck disable=SC1090
  source "$PREAMBLE_FILE"
  set -uo pipefail   # re-assert in case the preamble changed shell options
fi

# ── Phase 1: bootstrap — lock, update/clone the persistent metrics clone, re-exec ─────────────
if [ -z "${REG_FRESH:-}" ]; then
  mkdir -p "$WORK_DIR"
  # Portable single-instance lock (macOS has no flock): mkdir is atomic everywhere.  This lock is
  # $WORK_DIR-scoped, so it serialises runs of THIS nightly against each other.  Another
  # nightly on the same machine holds its lock in its own WORK_DIR.
  if ! mkdir "$WORK_DIR/.lock.d" 2>/dev/null; then
    log "another run holds the lock ($WORK_DIR/.lock.d) — exiting."; exit 0
  fi
  MC="$WORK_DIR/metrics"
  # A clone of a DIFFERENT repository must never be reused.  This directory was the mbirtorch
  # nightly's work dir while that nightly still lived in mbirjax_metrics, so an existing clone here
  # can point at the wrong origin.  Fetching into it would rebase this repository's work onto
  # another repository's history, and the re-exec below would then look for a wrapper that is not
  # there.  Replace it instead.
  if [ -d "$MC/.git" ]; then
    MC_ORIGIN="$(git -C "$MC" remote get-url origin 2>/dev/null || true)"
    if [ "${MC_ORIGIN%.git}" != "${METRICS_URL%.git}" ]; then
      log "metrics clone at $MC points at '${MC_ORIGIN:-<none>}', not '$METRICS_URL' — re-cloning."
      rm -rf "$MC"
    fi
  fi
  if [ -d "$MC/.git" ]; then
    log "updating metrics clone (fetch + rebase) -> $MC"
    if ! { git -C "$MC" fetch -q origin && git -C "$MC" pull -q --rebase --autostash; }; then
      log "metrics clone unusable — re-cloning fresh."
      rm -rf "$MC"
      git clone --quiet "$METRICS_URL" "$MC" || { log "FATAL: clone metrics failed."; rmdir "$WORK_DIR/.lock.d" 2>/dev/null; exit 2; }
    fi
  else
    log "cloning metrics -> $MC"
    git clone --quiet "$METRICS_URL" "$MC" || { log "FATAL: clone metrics failed."; rmdir "$WORK_DIR/.lock.d" 2>/dev/null; exit 2; }
  fi
  exec env REG_FRESH=1 "$MC/tooling/regression/run_regression.sh"
fi

# ── Phase 2: work — running from the persistent metrics clone ─────────────────────────────────
trap 'rm -rf "$WORK_DIR/.lock.d"' EXIT
METRICS_REPO="$(cd "$HERE/../.." && pwd)"        # = $WORK_DIR/metrics
HARNESS_DIR="$METRICS_REPO/tooling"

# Dedicated env (create if missing) + activate + harness deps; REG_VENV substitutes an
# existing venv for trial runs (see lib_env.sh).
# shellcheck disable=SC1091
source "$HARNESS_DIR/regression/lib_env.sh"
source "$HARNESS_DIR/regression/lib_ledger.sh"
reg_activate_env || exit $?

# Platform DECLARATION (nvidia-smi signal, no torch import).  The engine VERIFIES this claim
# against torch.cuda.is_available() and hard-aborts on disagreement — a GPU night on which CUDA
# cannot initialise becomes a loud failure, never a run silently filed under the other key.
# The lesson comes from the mbirjax nightly on 2026-07-21, which measured a whole GPU sweep on
# CPU and filed it under results/gpu/ with no other symptom.
PLAT="$(reg_plat)"
RES="$METRICS_REPO/results/$PLAT"; STATE="$METRICS_REPO/state/$PLAT"
mkdir -p "$RES" "$STATE"
log "platform=$PLAT env=${REG_VENV:-$CONDA_ENV} metrics=$METRICS_REPO"
LEDGER="$STATE/torch_ledger.txt"
CANARY_ON=0
case " ${DEP_CANARY_PLATFORMS:-gpu} " in *" $PLAT "*) [ "${DEP_CANARY_ENABLED:-0}" = "1" ] && CANARY_ON=1 ;; esac
# ── torch invariant: the env runs the ledger's newest validated torch, or the night does not run ─
# A restore that failed last night (rejection, walltime kill) is repaired here, and a repair that
# fails refuses to measure, so the env can never sit on a rejected torch unnoticed.
RUNS_ON="$(ledger_runs_on "$LEDGER")"
if [ -n "$RUNS_ON" ] && [ "${REG_SMOKE:-0}" != "1" ]; then
  _inst="$(reg_installed_torch)"
  if [ "$_inst" != "$RUNS_ON" ]; then
    log "torch invariant: env has torch '${_inst:-none}', the ledger says $RUNS_ON — re-pinning."
    if reg_pin_torch "$RUNS_ON" >"$WORK_DIR/pin_torch.log" 2>&1; then log "torch invariant: restored torch $RUNS_ON."
    else log "FATAL: could not restore torch $RUNS_ON (see $WORK_DIR/pin_torch.log) — refusing to measure."; exit 2; fi
  fi
fi

# Credential for unattended push (cluster), scoped to this repository only.
# GIT_TERMINAL_PROMPT=0 makes a missing credential fail fast instead of hanging the job.
export GIT_TERMINAL_PROMPT=0
if [ -n "${TOKEN_FILE:-}" ] && [ -f "$TOKEN_FILE" ]; then
  # RESET the helper list before adding ours.  git consults every configured credential.helper in
  # config order, system then global then local, and uses the FIRST credential returned.  A global
  # `credential.helper = store` reads ~/.git-credentials, which matches by HOST only, so its
  # github.com entry answers for every github.com request including this one.  On the cluster that
  # entry holds another repository's token, and the push fails with
  #     remote: Permission to cabouman/mbirtorch_metrics.git denied to <user>.
  # even though TOKEN_FILE holds a token that works.  An EMPTY first value resets the list, which
  # makes TOKEN_FILE the only helper for this repository.  Diagnosed 2026-08-24.
  git -C "$METRICS_REPO" config --local --replace-all credential.helper ""
  git -C "$METRICS_REPO" config --local --add credential.helper "store --file=$TOKEN_FILE"
fi

# ── Dependency-watch watchdog ─────────────────────────────────────────────────────────────────
# Design record: mbirtorch_plans/plans/torch_port/closed/python_matrix_nightly_check.md.
# THE WATCH is a scheduled GitHub Actions workflow in cabouman/mbirtorch.  It compares the CI
# Python test matrix and the torch floor against the Python versions torch publishes wheels for,
# and on a divergence it opens a ready-made pull request.  It can die without anyone noticing:
# GitHub disables a schedule after 60 days of repository quiet, scheduled runs are best-effort and
# can be skipped, and a failing scheduled run emails only whoever last touched the workflow file.
# None of those surfaces is watched daily, so "the watch found nothing" and "the watch is dead"
# look identical from the outside.
#
# THE WATCHDOG LINE is this nightly's independent second opinion.  It re-runs the watch's own
# checker (so detection is never reimplemented here), reads the repository's pull requests, and
# prints THREE FACTS SEPARATELY — the divergence, the pull-request state, the verdict — because a
# read that FAILED must never print as "no divergence".  Four verdicts:
#   quiet    no divergence; nothing for the watch to propose.
#   ok       a divergence, and its pull request is open or merged: the watch is working.
#   ALARM    a divergence with no open or merged pull request.  Two shapes, named in the line:
#            "(standing veto)"    = the pull request was closed unmerged — the maintainers' "no",
#                                   which must be reported every night until it lands;
#            "(no pull request)"  = the watch is broken, disabled, or has not yet reached the
#                                   two-night confirmation it waits for before acting.
#   UNKNOWN  the watchdog itself could not read something.  Distinct from every verdict above.
#
# Read-only and stateless.  It fetches nothing but the checker script; --remote then makes the
# checker read the version file and pyproject straight from the public prerelease branch, so no
# mbirtorch checkout is needed — and on an unchanged night there is none.  It passes no --state:
# the two-night confirmation file is the Action's own business, and the watchdog reports what is
# true TONIGHT.  With no --state and no --act the checker writes nothing anywhere.  Both network
# reads are unauthenticated because the repository is public (the cluster reaches both through the
# preamble's HTTPS_PROXY, which curl and python's urllib each honour from the environment).
#
# FAILURE ISOLATION is absolute: every step is captured and no step is fatal, and every step that
# touches the network is hard-bounded — 10 s to fetch the checker, 30 s to run it, 10 s for the
# pull-request read, so 50 s is the worst case (measured at 40 s with the checker hung and the API
# host blackholed).  Nothing here can change this script's exit status or delay the nightly.
#
# It runs on the CLUSTER ONLY.  PLAT is the wrapper's own platform signal: gautschi's nightly is
# gpu and the Mac's is cpu, and Greg ruled the slurm jobs the more reliable host for
# the backstop.  REG_NO_WATCHDOG=1 silences it without an edit.
#
# Placement: UNCONDITIONAL, above the no-change exit.  A watch that only reported on nights when
# some branch happened to move would not be a watchdog.
watchdog_line() {
  local tmp py ck url api slug sha out rc br detail torch_v raw code body cls tok
  local ck_pid waited why f_div f_pr verdict

  # WATCHDOG_CHECKER_URL / WATCHDOG_PR_API override the two endpoints; they exist so this block
  # can be exercised against a fixture without touching GitHub, and production leaves both unset.
  # The repository slug comes from MBIRTORCH_URL, so the watchdog always watches the same
  # repository this nightly measures.
  slug="$(printf '%s' "${MBIRTORCH_URL:-}" | sed -n 's#^https://github.com/\(.*\)\.git$#\1#p')"
  [ -n "$slug" ] || slug="cabouman/mbirtorch"
  url="${WATCHDOG_CHECKER_URL:-https://raw.githubusercontent.com/$slug/prerelease/ci/dependency_watch.py}"
  api="${WATCHDOG_PR_API:-https://api.github.com/repos/$slug/pulls}"

  tmp="$(mktemp -d 2>/dev/null)" || { log "watchdog: VERDICT UNKNOWN — no temp dir; the watchdog did not run."; return 0; }
  py="$(command -v python || command -v python3 || true)"

  # ── Fact 1: the divergence, from the watch's own checker ───────────────────────────────────
  ck="$tmp/dependency_watch.py"; sha="?"
  if [ -z "$py" ]; then
    f_div="UNKNOWN — no python on PATH to run the checker"
  elif ! curl -fsSL --max-time 10 "$url" -o "$ck" 2>"$tmp/curl.err"; then
    f_div="UNKNOWN — could not fetch the checker from $url ($(head -1 "$tmp/curl.err" 2>/dev/null || echo 'curl failed'))"
  else
    # Record WHICH checker ran: a watchdog that silently switched code is not a witness.
    if command -v sha256sum >/dev/null 2>&1; then sha="$(sha256sum "$ck" | cut -c1-12)"
    elif command -v shasum >/dev/null 2>&1;    then sha="$(shasum -a 256 "$ck" | cut -c1-12)"; fi
    # The checker's own network reads are urllib, not curl, so --max-time cannot reach them and
    # their 60 s-per-fetch timeout is well over this budget.  Bound it from outside instead: run
    # it in the background and kill it at 30 s.  Deliberately NOT `timeout`, which macOS does not
    # ship — a missing `timeout` would have removed the bound on exactly the host where a hang is
    # least expected and hardest to notice.  This poll needs nothing but bash, and it preserves
    # the checker's own exit status; 124 is kept as the killed-by-timeout code, as `timeout` uses.
    "$py" "$ck" --remote --json >"$tmp/checker.out" 2>&1 &
    ck_pid=$!; waited=0
    while kill -0 "$ck_pid" 2>/dev/null && [ "$waited" -lt 30 ]; do sleep 1; waited=$((waited + 1)); done
    if kill -0 "$ck_pid" 2>/dev/null; then
      kill -9 "$ck_pid" 2>/dev/null; wait "$ck_pid" 2>/dev/null; rc=124
    else
      wait "$ck_pid"; rc=$?
    fi
    out="$(cat "$tmp/checker.out" 2>/dev/null)"
    br="$(printf '%s\n' "$out" | sed -n 's/^dependency-watch: DIVERGENCE -> branch //p' | head -1)"
    torch_v="$(printf '%s\n' "$out" | sed -n 's/^dependency-watch: torch \([^ ]*\) supports.*$/\1/p' | head -1)"
    detail="$(printf '%s\n' "$out" | awk '/^dependency-watch:   /{sub(/^dependency-watch: +/,""); printf "%s%s", s, $0; s="; "}')"
    if [ "$rc" -ne 0 ]; then
      # 124 is the kill above; anything else is the checker's own failure (its "VERSION FILE NOT
      # READ" path exits 1 for exactly this reason).  Report the last line it managed to print.
      if [ "$rc" = "124" ]; then why="timed out after 30 s"
      else why="$(printf '%s\n' "$out" | grep -v '^$' | tail -1)"; fi
      f_div="UNKNOWN — the checker exited $rc (${why:-no output})"
    elif [ -n "$br" ]; then
      f_div="DUE on branch $br (torch ${torch_v:-?}; $detail)"
    elif printf '%s\n' "$out" | grep -q '^dependency-watch: verdict UNKNOWN'; then
      # The checker read the index and the matrix but not the torch ledger: the Python part is
      # checked, the torch floor and exclusions are not.  Not "no divergence".
      f_div="UNKNOWN — $(printf '%s\n' "$out" | sed -n 's/^dependency-watch: verdict UNKNOWN *//p' | head -1)"
    elif printf '%s\n' "$out" | grep -q '^dependency-watch: verdict none'; then
      f_div="none (torch ${torch_v:-?}; the matrix and both floors match)"
    else
      # The checker succeeded but said neither thing — its output shape changed under us.  This is
      # the case the three-facts rule exists for: it is NOT "no divergence".
      f_div="UNKNOWN — the checker reported neither a divergence nor 'verdict none'"
    fi
  fi

  # ── Fact 2: the pull-request state, from the public API ────────────────────────────────────
  # No -f: a 403 rate-limit or a 404 still has a readable body, and its message is reported as
  # itself.  The body and the status code come back in one call, newline-separated.
  cat >"$tmp/prs.py" <<'PY'
# Classify the repository's pull requests for the watchdog line.  Reads the API's JSON array on
# stdin; argv is the divergence's branch ("" when there is none) and the automated-branch prefix.
# Prints one line, "<token>|<human text>".
import json, sys
branch, prefix = sys.argv[1], sys.argv[2]
try:
    prs = json.load(sys.stdin)
    if not isinstance(prs, list):
        why = prs.get("message", "not a list") if isinstance(prs, dict) else "not a list"
        raise ValueError(why)
except Exception as e:
    print("unreadable|the pull-request list did not parse (%s)" % e)
    raise SystemExit(0)

def ref(p):
    return str((p.get("head") or {}).get("ref", ""))

def state(p):
    if p.get("merged_at"):
        return "MERGED"
    return "OPEN" if p.get("state") == "open" else "CLOSED UNMERGED"

def describe(ps):
    return ", ".join("#%d %s (%s)" % (p["number"], ref(p), state(p)) for p in ps)

read = "%d pull request%s read" % (len(prs), "" if len(prs) == 1 else "s")
if len(prs) >= 100:
    read += " (the 100 newest only; older ones were not read)"
auto = [p for p in prs if ref(p).startswith(prefix)]
# The plan's duplicate rule keys on the pull request, not the branch: merged or open means the
# watch acted, so prefer those over a closed one when a branch has several.
rank = {"MERGED": 0, "OPEN": 1, "CLOSED UNMERGED": 2}
mine = sorted((p for p in auto if branch and ref(p) == branch), key=lambda p: rank[state(p)])
if mine:
    p = mine[0]
    s = state(p)
    tok = {"MERGED": "merged", "OPEN": "open", "CLOSED UNMERGED": "closed"}[s]
    print("%s|%s; #%d for %s is %s" % (tok, read, p["number"], branch, s))
elif branch:
    others = describe(auto)
    tail = ("; other %s branches: " % prefix) + others if others else ""
    print("none|%s; NO pull request for %s%s" % (read, branch, tail))
else:
    listed = describe(auto)
    tail = ("%s pull requests: " % prefix) + listed if listed else "none on a %s branch" % prefix
    print("n/a|%s; %s" % (read, tail))
PY
  tok="unreadable"
  if [ -z "$py" ]; then
    f_pr="UNKNOWN — no python on PATH to parse the pull-request list"
  else
    raw="$(curl -sS --max-time 10 -H 'Accept: application/vnd.github+json' -w '\n%{http_code}' \
             "$api?state=all&per_page=100&sort=created&direction=desc" 2>&1)"; rc=$?
    code="${raw##*$'\n'}"; body="${raw%$'\n'*}"
    if [ "$rc" -ne 0 ]; then
      f_pr="UNKNOWN — the pull-request read failed: curl exited $rc ($(printf '%s' "$raw" | head -1))"
    elif [ "$code" != "200" ]; then
      f_pr="UNKNOWN — the pull-request read failed: HTTP $code from $api"
    else
      cls="$(printf '%s' "$body" | "$py" "$tmp/prs.py" "${br:-}" "nightly/" 2>&1)"
      case "$cls" in
        *"|"*) tok="${cls%%|*}"; f_pr="${cls#*|}" ;;
        *)     f_pr="UNKNOWN — the pull-request read could not be classified ($cls)" ;;
      esac
      [ "$tok" = "unreadable" ] && f_pr="UNKNOWN — the pull-request read failed: $f_pr"
    fi
  fi

  # ── Fact 3: the verdict ────────────────────────────────────────────────────────────────────
  case "$f_div" in
    UNKNOWN*) verdict="UNKNOWN — the watchdog could not check tonight; the watch's health is unproven, which is NOT the same as healthy." ;;
    none*)    case "$f_pr" in
                UNKNOWN*) verdict="UNKNOWN — no divergence was found, but the pull-request read failed, so a suppressed one cannot be ruled out." ;;
                *)        verdict="quiet — no divergence tonight, so the watch has nothing to propose." ;;
              esac ;;
    *)        case "$tok" in
                open|merged) verdict="ok — a divergence, and its pull request is open or merged: the watch is working." ;;
                closed)      verdict="ALARM (standing veto) — a divergence whose pull request was closed unmerged: the maintainers' standing \"no\".  Deleting the branch withdraws it." ;;
                none)        verdict="ALARM (no pull request) — a divergence with no pull request at all: the watch is broken, disabled, or has not yet reached its two-night confirmation.  Check the dependency_watch workflow's runs in $slug." ;;
                *)           verdict="UNKNOWN — a divergence was found, but the pull-request read failed, so it is not known whether the watch acted." ;;
              esac ;;
  esac

  log "watchdog: checker $url @ sha256 $sha"
  log "watchdog: divergence: $f_div"
  log "watchdog: pull requests: $f_pr"
  log "watchdog: VERDICT $verdict"
  rm -rf "$tmp"
  return 0
}

if [ "${REG_NO_WATCHDOG:-0}" = "1" ]; then
  log "watchdog: skipped (REG_NO_WATCHDOG=1) — no verdict tonight."
elif [ "$PLAT" != "gpu" ]; then
  log "watchdog: not run on $PLAT — the cluster (gpu) nightly owns it."
else
  # The `|| true` is belt to the function's own braces: the watchdog can never fail the nightly.
  watchdog_line || true
fi

# ── torch-release watch (best-effort, NON-FATAL, runs every night even on no-change) ──────────
# Warn when a torch newer than TORCH_LAST_REVIEWED ships, so a release that changes performance
# gets re-tested with measure_one_cell.py instead of riding in silently on the next clean install.
# The companion --headroom line says whether that newer torch can even install on this env's
# Python.  Both are captured rather than only printed, so the notify email below can carry them.
# This block sits BEFORE the no-change exit, so it fires regardless of branch activity.
ENV_PY="$(python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
TORCHWATCH=""; HEADROOM=""
# TORCH_LAST_REVIEWED left empty means "the ledger's newest version" (validated, rejected or
# unavailable alike), so the line quiets itself once the canary has reacted to a release.
[ -n "${TORCH_LAST_REVIEWED:-}" ] || TORCH_LAST_REVIEWED="$(ledger_newest "$LEDGER")"
if [ -n "${TORCH_LAST_REVIEWED:-}" ] && [ -f "$HERE/check_torch_release.py" ]; then
  TORCHWATCH="$(python "$HERE/check_torch_release.py" "$TORCH_LAST_REVIEWED" 2>/dev/null || true)"
  # Diagnose headroom ONLY when a newer torch exists, so bumping TORCH_LAST_REVIEWED quiets both
  # lines.  An already-reviewed, held-back torch does not nag nightly, and the pair re-appears the
  # moment a genuinely new torch ships.
  if [ -n "$TORCHWATCH" ]; then
    log "$TORCHWATCH"
    HEADROOM="$(python "$HERE/check_torch_release.py" --headroom "$ENV_PY" 2>/dev/null || true)"
    [ -n "$HEADROOM" ] && log "$HEADROOM"
  fi
fi

# ── Change detection via ls-remote (don't clone mbirtorch unless something moved) ──────────────
# EMPTY-ARRAY EXPANSION.  macOS ships bash 3.2, where `"${arr[@]}"` on an EMPTY array raises
# "unbound variable" under `set -u` and aborts the script; `${#arr[@]}` is safe, and bash 4.4 and
# later are safe both ways.  Every expansion of an array that can be empty therefore uses the
# `${arr[@]+"${arr[@]}"}` form, which expands to nothing when the array is empty.  This is not
# hypothetical: the same construct aborted the mbirjax macOS nightly on every run from 2026-08-18,
# and the only symptom was a log that stopped mid-run.
CHANGED_BR=()
for BR in ${TRACKED_BRANCHES[@]+"${TRACKED_BRANCHES[@]}"}; do
  SHA="$(git ls-remote "$MBIRTORCH_URL" "refs/heads/$BR" 2>/dev/null | awk '{print $1}')"
  [ -n "$SHA" ] || { log "skip $BR: not found on remote."; continue; }
  SLUG="${BR//\//_}"
  LAST="$(cat "$STATE/$SLUG" 2>/dev/null || true)"
  if [ "$SHA" = "$LAST" ] && [ "${REG_FORCE:-0}" != "1" ]; then
    log "$BR @ ${SHA:0:8}: unchanged — skip."
  else
    [ -n "$LAST" ] && WAS="${LAST:0:8}" || WAS="none"
    if [ "$SHA" = "$LAST" ]; then
      log "$BR @ ${SHA:0:8}: unchanged but REG_FORCE=1 — re-measuring."
    else
      log "$BR @ ${SHA:0:8}: CHANGED (was $WAS)."
    fi
    CHANGED_BR+=("$BR")
  fi
done

# ── Dependency canary: a NEW torch counts as a change ─────────────────────────────────────────
# Guarded by DEP_CANARY_ENABLED and DEP_CANARY_PLATFORMS.  The candidate is what the PINNED WHEEL
# INDEX would install; PyPI's newest matters only when the index lags it, which becomes an
# `unavailable` ledger line and a mail.  A candidate is NEW while its latest ledger line is neither
# `validated` nor `rejected` (missing, candidate, unavailable all re-fire, so a night killed between
# the candidate line and the verdict is retried; the old torch_seen files are retired).  The env is
# upgraded FIRST, the release counts only when the installed torch IS the candidate, and the verdict
# run further down decides BEFORE any other branch is measured.  Design record: the plans repo's
# plans/mbirtorch_metrics/torch_version_policy_plan.md.
NEW_TORCH=0; TORCH_EFFECTIVE=0; FULL_REFRESH=0; DEP_GEN=""; FULL_GEN=""; RAN_TORCH_STEP=0
PYPI_TORCH=""; IDX_TORCH=""; CANDIDATE=""; CANARY=""; CANARY_PREV=""; CANARY_TIP=""; CANARY_ADDED=0
LEDGER_DIRTY=0; LEDGER_ALERT=""
_newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | _ledger_vsort_desc | head -1)" = "$1" ]; }
if [ "$CANARY_ON" = "1" ] && [ -f "$HERE/check_torch_release.py" ]; then
  CANARY="${DEP_CANARY_BRANCH:-main}"; _g="$(cat "$STATE/depgen" 2>/dev/null || echo 0)"
  PYPI_TORCH="$(python "$HERE/check_torch_release.py" --print-latest 2>/dev/null || true)"
  IDX_TORCH="$(reg_index_newest_torch)"
  CANDIDATE="${IDX_TORCH:-$PYPI_TORCH}"
  if [ -n "$PYPI_TORCH" ] && [ -n "$IDX_TORCH" ] && _newer "$PYPI_TORCH" "$IDX_TORCH"; then
    if ledger_append "$LEDGER" "$PYPI_TORCH" unavailable "index=$(reg_torch_index) index-newest=$IDX_TORCH python=$ENV_PY"; then
      LEDGER_DIRTY=1
      LEDGER_ALERT="torch $PYPI_TORCH is on PyPI but the pinned wheel index ($(reg_torch_index)) stops at $IDX_TORCH: TORCH_INDEX_URL_$PLAT in run_configs.env may need to move (check the driver's CUDA ceiling in the cluster probe's facts first).  Ledger: $LEDGER"
    fi
  fi
  _st="$(ledger_state "$LEDGER" "$CANDIDATE")"
  if [ -n "$CANDIDATE" ] && [ "$CANDIDATE" != "$RUNS_ON" ] && [ "$_st" != "validated" ] && [ "$_st" != "rejected" ]; then NEW_TORCH=1; fi
  _last="$(cat "$STATE/last_full_refresh" 2>/dev/null || echo 0)"
  _now="$(date +%s)"
  _days="${DEP_FULL_REFRESH_DAYS:-14}"
  [ "$(( (_now - _last) / 86400 ))" -ge "$_days" ] && FULL_REFRESH=1
  if [ "$NEW_TORCH" = "1" ] && [ "${REG_SMOKE:-0}" != "1" ]; then
    log "dep-canary: torch $CANDIDATE is the index's newest (ledger: ${_st:-none}; PyPI ${PYPI_TORCH:-?}) — upgrading in ${REG_VENV:-$CONDA_ENV} ..."
    if reg_upgrade_torch >"$WORK_DIR/upgrade_torch.log" 2>&1 && [ "$(reg_installed_torch)" = "$CANDIDATE" ]; then
      TORCH_EFFECTIVE=1
      ledger_append "$LEDGER" "$CANDIDATE" candidate "index python=$ENV_PY" && LEDGER_DIRTY=1
      log "dep-canary: env now runs torch $CANDIDATE (candidate); the verdict run decides tonight."
    else
      _inst="$(reg_installed_torch)"
      log "dep-canary: the upgrade did not land torch $CANDIDATE (env has ${_inst:-none}; see $WORK_DIR/upgrade_torch.log) — no verdict tonight, retry tomorrow."
      if [ -n "$RUNS_ON" ] && [ "${_inst:-}" != "$RUNS_ON" ]; then   # hold the invariant
        if reg_pin_torch "$RUNS_ON" >>"$WORK_DIR/upgrade_torch.log" 2>&1; then log "dep-canary: env held at torch $RUNS_ON."
        else log "FATAL: env left on torch ${_inst:-none} and could not restore $RUNS_ON — refusing to measure."; exit 2; fi
      fi
    fi
  fi
  if [ "$TORCH_EFFECTIVE" = "1" ] || [ "$FULL_REFRESH" = "1" ]; then
    DEP_GEN="$(( _g + 1 ))"; FULL_GEN="$DEP_GEN"
    CANARY_TIP="$(git ls-remote "$MBIRTORCH_URL" "refs/heads/$CANARY" 2>/dev/null | awk '{print $1}')"
    CANARY_PREV="$(cat "$STATE/${CANARY//\//_}" 2>/dev/null || true)"
    # Both changed: a new torch AND a moved canary tip.  The verdict run measures the PREVIOUS tip
    # under the new torch, which isolates the dependency; the loop then measures the new tip.
    [ "$TORCH_EFFECTIVE" = "1" ] && [ -n "$CANARY_PREV" ] && [ -n "$CANARY_TIP" ] && \
      [ "$CANARY_PREV" != "$CANARY_TIP" ] && RAN_TORCH_STEP=1
    log "dep-canary: TORCH_EFFECTIVE=$TORCH_EFFECTIVE FULL=$FULL_REFRESH  gen=$DEP_GEN  prev=${CANARY_PREV:0:8} tip=${CANARY_TIP:0:8}"
    if [ "$TORCH_EFFECTIVE" = "1" ]; then   # keep the night alive for the verdict run
      _in=0; for _b in ${CHANGED_BR[@]+"${CHANGED_BR[@]}"}; do [ "$_b" = "$CANARY" ] && _in=1; done
      [ "$_in" = "0" ] && [ -n "$CANARY_TIP" ] && { CHANGED_BR+=("$CANARY"); CANARY_ADDED=1; }
    fi
  fi
fi

# Proceed when a branch changed, the canary has a step to run, or the ledger gained a line that
# must be published — an `unavailable` written on a quiet night would otherwise never be committed.
if [ "${#CHANGED_BR[@]}" -eq 0 ] && [ "$FULL_REFRESH" != "1" ]; then
  if [ "$LEDGER_DIRTY" = "1" ]; then
    log "no tracked branch changed — publishing the ledger only."
  else
    log "no tracked branch changed — done."; exit 0
  fi
fi

DATE="$(date '+%Y%m%d')"
GATE_FAIL=0
TEST_FAIL=0
# Alert email: accumulate per-branch hard-gate items + test failures, sent ONCE at the end.  The
# exit code, and therefore slurm's --mail-type=FAIL mail, tracks GATE_FAIL only.  A test-only
# failure emails its detail WITHOUT flipping the exit code.
ALERT_BODY="$(mktemp)"
MAIL_TO="${REG_MAIL_TO:-buzzard@purdue.edu}"

# measure_commit: run the engine for ONE (branch, sha) at a given dependency generation and reason.
# This is the vehicle for the canary steps that are not the plain per-branch loop below, which are
# the torch step on the PREVIOUS canary tip and the full-dependency refresh.  It clones that exact
# sha into a throwaway directory, fetching it directly when it is older than the tip, installs it,
# runs the engine, and cleans up.  It runs no tests and writes no branch state; the caller owns
# those.  It returns the engine's exit code.
measure_commit() {   # $1=branch $2=sha $3=outdir $4=dep_gen $5=reason $6=upgrade(none|full) [$7=tests 0|1] [$8=engine log]
  local _br="$1" _sha="$2" _out="$3" _dg="$4" _reason="$5" _upg="${6:-none}" _tests="${7:-0}" _elog="${8:-}" _rc=0
  local _wt="$WORK_DIR/canary_${_br//\//_}_${_sha:0:8}"; rm -rf "$_wt"
  MC_ENGINE_RC=""; MC_ABORTED=0; MC_WARN=0; MC_WARN_PERF=0; MC_TESTS_OK=""; MC_RUN=""   # the step's outcome
  if ! git clone --quiet --depth 1 --branch "$_br" --single-branch "$MBIRTORCH_URL" "$_wt"; then
    log "  dep-canary: clone of $_br failed — skip step."; rm -rf "$_wt"; return 2; fi
  if [ "$(git -C "$_wt" rev-parse HEAD)" != "$_sha" ]; then   # older than the tip -> fetch that commit
    if ! { git -C "$_wt" fetch --quiet --depth 1 origin "$_sha" && git -C "$_wt" checkout --quiet "$_sha"; }; then
      log "  dep-canary: could not check out ${_sha:0:8} of $_br — skip step."; rm -rf "$_wt"; return 2; fi
  fi
  local _ilog="$WORK_DIR/install_canary_${_br//\//_}.log"
  if [ "$_upg" = "full" ]; then
    reg_upgrade_all "$_wt" "$(reg_installed_torch)" >"$_ilog" 2>&1 || log "  dep-canary: WARN full upgrade issue (see $_ilog)."
  elif ! reg_install_lib "$_wt" >"$_ilog" 2>&1; then
    log "  dep-canary: install of ${_sha:0:8} failed (see $_ilog) — skip step."; rm -rf "$_wt"; return 2; fi
  mkdir -p "$_out"
  if [ "$_tests" = "1" ] && [ "${RUN_TESTS:-0}" = "1" ]; then
    if canary_run_suite "$_wt" "$_out/tests_${PLAT}_${DATE}_${_reason}.txt" "$_br @ ${_sha:0:8} ($_reason, torch $(reg_installed_torch))"; then MC_TESTS_OK=1; else MC_TESTS_OK=0; fi
  fi
  [ -n "$_elog" ] || _elog="$(mktemp)"
  REG_LIB_ROOT="$_wt" REG_OUT_DIR="$_out" REG_DATE="$DATE" REG_GATE=1 REG_RUN_TAG="$_br" \
    REG_PLATFORM="$PLAT" REG_SKIP_TESTS=1 REG_DEP_GEN="$_dg" REG_RUN_REASON="$_reason" \
    REG_TORCH_AVAILABLE="$PYPI_TORCH" REG_DEVICE_COUNTS="${DEVICE_COUNTS:-1}" \
    REG_MEM_GATE_WINDOW="${MEM_GATE_WINDOW:-}" \
    python "$HARNESS_DIR/scaling_tests/performance_tracking.py" 2>&1 | tee "$_elog"
  _rc="${PIPESTATUS[0]}"
  MC_ENGINE_RC="$_rc"
  grep -qaE "produced no result|CUDA error|CUDA_ERROR|PLATFORM MISMATCH|DEVICE PIN FAILED" "$_elog" 2>/dev/null && MC_ABORTED=1
  grep -qa "GATE: WARN" "$_elog" 2>/dev/null && MC_WARN=1
  # A WARN counts against the release only when a soft line reports a PERFORMANCE change (time,
  # speedup, memory) against the prior.  "new cell, no baseline" and "gained sharding" notes are
  # informational: the trial of 2026-09-03 rejected torch 2.14.0 on eighteen new-cell notes left
  # by a widened sweep, which is exactly the false rejection this distinction prevents.
  MC_WARN_PERF=0
  if grep -aE '^[[:space:]]*warn[[:space:]]' "$_elog" 2>/dev/null | grep -avE 'new cell|no baseline|gained sharding|gained banding' \
       | grep -qaE '[[:space:]](time|speedup|mem)[[:space:]]'; then MC_WARN_PERF=1; fi
  MC_RUN="$(ls -t "$_out"/regression_${PLAT}_*.yaml 2>/dev/null | grep -v '_table\.yaml$' | head -1 || true)"
  rm -rf "$_wt"; return "$_rc"
}

# canary_run_suite WT LOG LABEL : the mbirtorch suite for a canary step, by the loop's rules (crash
# markers count as failure); appends to ALERT_BODY; returns 0 ok, 1 failures, 2 crashed.
canary_run_suite() {
  local _wt="$1" _tlog="$2" _label="$3" _trc _crashed _np
  _np=$([ "$PLAT" = "gpu" ] && echo 4 || echo 8)
  log "dep-canary: running the mbirtorch suite for $_label (xdist -n $_np) -> $(basename "$_tlog") ..."
  if [ -f "$_wt/dev_scripts/run_tests.sh" ]; then
    ( cd "$_wt/dev_scripts" && PYTEST_NPROC="$_np" bash run_tests.sh ) 2>&1 | tee "$_tlog"
  else
    ( cd "$_wt" && python -m pytest tests -ra -n "$_np" ) 2>&1 | tee "$_tlog"
  fi
  _trc="${PIPESTATUS[0]}"
  if grep -qaE "maximum crashed workers reached|Fatal Python error|INTERNALERROR|node down" "$_tlog" 2>/dev/null; then _crashed=1; else _crashed=0; fi
  if [ "$_trc" -eq 0 ] && [ "$_crashed" -eq 0 ]; then log "dep-canary: tests done."; return 0; fi
  TEST_FAIL=1
  { echo "### dep-canary $_label — $([ "$_crashed" = 1 ] && echo 'TEST RUN CRASHED' || echo 'TEST FAILURES')"
    grep -aE "^FAILED |[0-9]+ (failed|error)|maximum crashed workers reached|Fatal Python error|node down|INTERNALERROR" "$_tlog" | sort -u | tail -20
    echo; } >>"$ALERT_BODY"
  if [ "$_crashed" = 1 ]; then return 2; else return 1; fi
}

# (The torch upgrade happened in the canary block above, BEFORE the decision to measure.)
# ── dep-canary: the verdict run, BEFORE the branch loop ─────────────────────────────────────
# One measurement of the canary branch under the candidate torch decides.  With an unmoved tip it
# IS the tip under the new torch (torch-step); with a moved tip it is the PREVIOUS tip, so the
# dependency is isolated and the loop below measures the new tip as the code step.  Rejection (or
# no outcome) restores the ledger's torch, sets the verdict run aside, and untags the night, so no
# other branch is ever measured under a torch the ledger does not hold as validated.
CANARY_ELOG="$WORK_DIR/canary_engine.log"
if [ "$TORCH_EFFECTIVE" = "1" ] && [ "${REG_SMOKE:-0}" != "1" ]; then
  _sha="$CANARY_TIP"; _reason="torch-step"
  [ "$RAN_TORCH_STEP" = "1" ] && _sha="$CANARY_PREV"
  log "dep-canary: verdict run — $CANARY @ ${_sha:0:8} under torch $CANDIDATE ($_reason, gen $DEP_GEN)."
  _rc=0; measure_commit "$CANARY" "$_sha" "$RES/${CANARY//\//_}" "$DEP_GEN" "$_reason" none 1 "$CANARY_ELOG" || _rc=$?
  _inst="$(reg_installed_torch)"; _why=""; _outcome=1
  if [ -z "$MC_ENGINE_RC" ]; then
    _outcome=0
    log "dep-canary: NO OUTCOME (setup rc=$_rc: clone or install failed) — the candidate line stays and tomorrow retries."
  elif [ "$_inst" != "$CANDIDATE" ]; then
    _why="measured under torch ${_inst:-none}, not $CANDIDATE (the branch's own pyproject holds torch back)"
  else
    [ "$MC_ABORTED" = "1" ] && _why="engine aborted"
    [ "$MC_ENGINE_RC" = "1" ] && _why="${_why:+$_why; }hard gate FAIL"
    [ "$MC_ENGINE_RC" -ge 2 ] 2>/dev/null && _why="${_why:+$_why; }engine rc=$MC_ENGINE_RC"
    [ "$MC_TESTS_OK" = "0" ] && _why="${_why:+$_why; }tests failed"
    [ "$MC_WARN_PERF" = "1" ] && [ "${DEP_CANARY_WARN_IS_REJECT:-1}" = "1" ] && _why="${_why:+$_why; }gate WARN on time/speedup/memory"
  fi
  _ev="$(basename "${MC_RUN:-no-run-file}")"
  if [ "$MC_WARN_PERF" = "1" ]; then _ev="$_ev warn=perf"; elif [ "$MC_WARN" = "1" ]; then _ev="$_ev warn=info"; fi
  if [ "$MC_WARN" = "1" ]; then   # a soft warning is visible whichever way the verdict goes
    { echo "### dep-canary $CANARY @ ${_sha:0:8} under torch $CANDIDATE — GATE WARN lines"
      grep -aE "GATE: WARN|^ *SOFT |warn" "$CANARY_ELOG" | head -25; echo; } >>"$ALERT_BODY"
  fi
  if [ "$_outcome" = "0" ]; then
    :
  elif [ -z "$_why" ]; then
    ledger_append "$LEDGER" "$CANDIDATE" validated "$_ev python=$ENV_PY" && LEDGER_DIRTY=1
    RUNS_ON="$CANDIDATE"
    log "dep-canary: torch $CANDIDATE VALIDATED ($_ev) — the nightly runs on it from now on."
    [ "$MC_WARN_PERF" = "1" ] && LEDGER_ALERT="torch $CANDIDATE validated WITH a performance WARN (DEP_CANARY_WARN_IS_REJECT=0); the warn lines follow.  Ledger: $LEDGER"
  else
    ledger_append "$LEDGER" "$CANDIDATE" rejected "$_ev: $_why python=$ENV_PY" && LEDGER_DIRTY=1
    log "dep-canary: torch $CANDIDATE REJECTED ($_why)."
    LEDGER_ALERT="torch $CANDIDATE REJECTED by the canary: $_why.  Env restored to ${RUNS_ON:-?}; the verdict run is set aside under results/$PLAT/${CANARY//\//_}/rejected/.  To accept the release anyway, edit its line in $LEDGER to 'validated' (the next start re-pins the env); to re-test it, change the line to 'candidate'."
    [ "$MC_ENGINE_RC" = "1" ] && { echo "### dep-canary $CANARY @ ${_sha:0:8} under torch $CANDIDATE — GATE FAIL"
      grep -aE "GATE: FAIL|^ *HARD " "$CANARY_ELOG" | head -25; echo; } >>"$ALERT_BODY"
    if [ -n "$MC_RUN" ] && [ -f "$MC_RUN" ]; then
      mkdir -p "$(dirname "$MC_RUN")/rejected"
      for _f in "$MC_RUN" "${MC_RUN%.yaml}_table.yaml"; do [ -f "$_f" ] && mv -f "$_f" "$(dirname "$MC_RUN")/rejected/"; done
      log "dep-canary: set aside $(basename "$MC_RUN") (and its table) under rejected/."
    fi
  fi
  if [ "$_outcome" = "0" ] || [ -n "$_why" ]; then   # the rest of the night runs on the ledger's torch
    if [ -n "$RUNS_ON" ] && [ "$(reg_installed_torch)" != "$RUNS_ON" ]; then
      if reg_pin_torch "$RUNS_ON" >"$WORK_DIR/pin_torch.log" 2>&1; then log "dep-canary: env restored to torch $RUNS_ON."
      else log "FATAL: could not restore torch $RUNS_ON (see $WORK_DIR/pin_torch.log) — not measuring the other branches tonight."; GATE_FAIL=1; CHANGED_BR=(); fi
    fi
    TORCH_EFFECTIVE=0; [ "$FULL_REFRESH" = "1" ] || DEP_GEN=""
  fi
  # An unmoved canary tip was in tonight's list only for the verdict run; the loop need not repeat it.
  if [ "$CANARY_ADDED" = "1" ]; then
    _keep=(); for _b in ${CHANGED_BR[@]+"${CHANGED_BR[@]}"}; do [ "$_b" = "$CANARY" ] || _keep+=("$_b"); done
    CHANGED_BR=(${_keep[@]+"${_keep[@]}"})
  fi
fi

# dep-canary full refresh: eager-upgrade every dependency and re-measure the canary tip, which
# catches drift in packages other than torch.
if [ "$FULL_REFRESH" = "1" ] && [ -n "$CANARY_TIP" ] && [ "${REG_SMOKE:-0}" != "1" ]; then
  log "dep-canary: full refresh — re-measuring $CANARY @ ${CANARY_TIP:0:8} with all deps upgraded (gen $FULL_GEN)."
  measure_commit "$CANARY" "$CANARY_TIP" "$RES/${CANARY//\//_}" "$FULL_GEN" "deps-step" full \
    || log "dep-canary: full refresh returned non-zero (gate or setup) — continuing."
  date +%s >"$STATE/last_full_refresh"
fi

for BR in ${CHANGED_BR[@]+"${CHANGED_BR[@]}"}; do
  SLUG="${BR//\//_}"
  WT="$WORK_DIR/lib_$SLUG"; rm -rf "$WT"
  log "$BR: shallow-cloning the mbirtorch tip -> $WT"
  if ! git clone --quiet --depth 1 --branch "$BR" --single-branch "$MBIRTORCH_URL" "$WT"; then
    log "ERROR $BR: shallow clone failed — skip."; continue
  fi
  SHA="$(git -C "$WT" rev-parse HEAD)"   # the tip we actually got; recorded as state below

  # The install log lives OUTSIDE the clone, for two reasons.  Inside the clone it survives
  # neither path: a failed install deletes the clone with the log in it, and on success the
  # untracked file makes git_provenance read the pristine origin tip as git_dirty: true
  # (mbirtorch's .gitignore has no *.log rule), stamping a false dirty badge on every nightly
  # row.
  ILOG="$WORK_DIR/install_${SLUG}.log"
  log "$BR: installing mbirtorch [$INSTALL_EXTRAS] into ${REG_VENV:-$CONDA_ENV} ..."
  if ! reg_install_lib "$WT" >"$ILOG" 2>&1; then
    log "ERROR $BR: install of '$WT' failed (see $ILOG) — skip."
    rm -rf "$WT"; continue
  fi

  # REG_SMOKE = isolated plumbing test: a toy 1-cell sweep into a TEMP dir; skip tests /
  # commit / push / state below — never touches real data.
  if [ "${REG_SMOKE:-0}" = "1" ]; then
    OUT="$(mktemp -d)"; log "$BR: REG_SMOKE — toy output to $OUT (skipping tests / commit / push / state)."
  else
    OUT="$RES/$SLUG"
  fi
  mkdir -p "$OUT"

  # Tests FIRST, then the engine.  NON-FATAL (logged and emailed, not gated).  The wrapper owns
  # this step, for the live output, the crash markers, and the alert lines; the engine's own
  # built-in suite run is switched off below via REG_SKIP_TESTS.
  if [ "${RUN_TESTS:-0}" = "1" ] && [ "${REG_SMOKE:-0}" != "1" ]; then
    TLOG="$OUT/tests_${PLAT}_${DATE}.txt"
    # xdist workers: 4 on GPU (concurrent CUDA inits abort in numbers), 8 on CPU.
    NPROC=$([ "$PLAT" = "gpu" ] && echo 4 || echo 8)
    log "$BR: running the mbirtorch suite (xdist -n $NPROC) -> $(basename "$TLOG") ..."
    if [ -f "$WT/dev_scripts/run_tests.sh" ]; then
      # run_tests.sh resolves ../tests RELATIVE to dev_scripts/, so it must run from there.
      ( cd "$WT/dev_scripts" && PYTEST_NPROC="$NPROC" bash run_tests.sh ) 2>&1 | tee "$TLOG"
    else
      ( cd "$WT" && python -m pytest tests -ra -n "$NPROC" ) 2>&1 | tee "$TLOG"
    fi
    tests_rc="${PIPESTATUS[0]}"
    # A wholesale crash (xdist workers dying at import) can exit 0 with zero FAILED lines — scan
    # for crash markers so a run where NO tests ran cannot look green (the 2026-07-10 lesson).
    if grep -qaE "maximum crashed workers reached|Fatal Python error|INTERNALERROR|node down" "$TLOG" 2>/dev/null; then crashed=1; else crashed=0; fi
    if [ "$tests_rc" -eq 0 ] && [ "$crashed" -eq 0 ]; then
      log "$BR: tests done."
    elif [ "$crashed" -eq 1 ]; then
      TEST_FAIL=1
      log "$BR: TEST RUN CRASHED — xdist workers died; NO test results (emailed; see $(basename "$TLOG"))."
      { echo "### $BR @ ${SHA:0:8} — TEST RUN CRASHED (xdist workers died — no results)"
        grep -aE "maximum crashed workers reached|Fatal Python error|node down|INTERNALERROR" "$TLOG" | sort -u | head -6
        echo; } >>"$ALERT_BODY"
    else
      TEST_FAIL=1
      log "$BR: tests reported failures (non-fatal; emailed — see $(basename "$TLOG"))."
      { echo "### $BR @ ${SHA:0:8} — TEST FAILURES"
        grep -aE "^FAILED |[0-9]+ (failed|error)" "$TLOG" | tail -20
        echo; } >>"$ALERT_BODY"
    fi
  fi

  # The engine (gate + records + dashboard files).  It re-verifies the platform claim, pins
  # and asserts the device count on every row, and exits 1 on a hard-gate regression / >=2 on a
  # setup failure.  MBIRTORCH_MEMORY_CALIBRATION must be absent (the engine asserts it too).
  # Tag the run with tonight's dependency generation when the env changed under it (a validated
  # new torch, or the periodic refresh), and say why the canary branch is measured: code-step is
  # the moved tip under the new torch after the verdict run measured the previous tip.
  _reason="commit"
  [ -n "$DEP_GEN" ] && [ "$TORCH_EFFECTIVE" = "1" ] && [ "$BR" = "$CANARY" ] && _reason="code-step"
  log "$BR: running the measurement engine (output follows)..."
  GLOG="$(mktemp)"
  unset MBIRTORCH_MEMORY_CALIBRATION
  REG_LIB_ROOT="$WT" REG_OUT_DIR="$OUT" REG_DATE="$DATE" REG_GATE=1 \
       REG_RUN_TAG="$BR" REG_PLATFORM="$PLAT" REG_SKIP_TESTS=1 \
       REG_DEVICE_COUNTS="${DEVICE_COUNTS:-1}" \
       REG_MEM_GATE_WINDOW="${MEM_GATE_WINDOW:-}" \
       REG_DEP_GEN="${DEP_GEN:-}" REG_RUN_REASON="$_reason" REG_TORCH_AVAILABLE="${PYPI_TORCH:-}" \
       python "$HARNESS_DIR/scaling_tests/performance_tracking.py" 2>&1 | tee "$GLOG"
  engine_rc="${PIPESTATUS[0]}"
  # The engine can in principle print an abort yet exit 0, so catch CUDA and setup markers from
  # the output too.  A no-record night must be loud, not silently green.  The setup marker names
  # the setup worker: a failed automatic-device-choice check prints "the auto-choice worker
  # produced no result", but that run still writes its record, so it is a gate failure (exit 1),
  # not an abort.
  if grep -qaE "setup worker produced no result|CUDA error|CUDA_ERROR|PLATFORM MISMATCH|DEVICE PIN FAILED" "$GLOG" 2>/dev/null; then engine_aborted=1; else engine_aborted=0; fi
  if [ "$engine_rc" -eq 0 ] && [ "$engine_aborted" -eq 0 ]; then
    log "$BR: engine ok."
  elif [ "$engine_rc" -eq 1 ] && [ "$engine_aborted" -eq 0 ]; then
    GATE_FAIL=1; log "$BR: GATE FAIL (regression) — see $OUT."
    { echo "### $BR @ ${SHA:0:8} — GATE FAIL (vs prior baseline)"
      grep -aE "GATE: FAIL|^ *HARD " "$GLOG" | head -25
      echo; } >>"$ALERT_BODY"
  else
    TEST_FAIL=1
    log "$BR: ENGINE ABORTED (rc=$engine_rc) — no usable record; emailed."
    { echo "### $BR @ ${SHA:0:8} — TORCH ENGINE ABORTED (rc=$engine_rc; no record -> no dashboard entry)"
      grep -aE "setup worker produced no result|CUDA error|CUDA_ERROR|PLATFORM MISMATCH|DEVICE PIN FAILED|Traceback" "$GLOG" | sort -u | head -6
      echo; } >>"$ALERT_BODY"
  fi
  rm -f "$GLOG"

  # Record the measured commit LAST, and only when the engine produced a record (rc 0 or 1 — a
  # gate FAIL still measured; an abort re-measures next run instead of being marked done).
  if [ "${REG_SMOKE:-0}" != "1" ] && [ "$engine_aborted" != "1" ] && [ "$engine_rc" -le 1 ]; then
    echo "$SHA" >"$STATE/$SLUG"
  fi
  rm -rf "$WT"
done

# dep-canary bookkeeping: the dependency generation.  torch_seen / torch_seen_python are retired —
# the ledger is the record of what was reacted to.
if [ -n "$DEP_GEN" ] && [ "${REG_SMOKE:-0}" != "1" ]; then
  echo "$DEP_GEN" >"$STATE/depgen"
fi
rm -f "$STATE/torch_seen" "$STATE/torch_seen_python"

# ── Publish to the metrics repo (conflict-safe; NON-FATAL) ────────────────────────────────────
# This wrapper stages ONLY its own platform's paths (results/<plat>/, state/<plat>/), so the GPU
# and CPU nightlies conflict only at the git level, where the rebase-retry below resolves them.
# A failed push self-heals: state was not pushed, so the next run re-measures.  Skipped under REG_SMOKE and under REG_NO_PUSH (the trial seam).
if [ "${REG_SMOKE:-0}" = "1" ]; then
  log "REG_SMOKE — skipping commit/push."
elif [ "${REG_NO_PUSH:-0}" = "1" ]; then
  log "REG_NO_PUSH — results committed locally are NOT pushed (trial seam)."
else
git -C "$METRICS_REPO" add "results/$PLAT" "state/$PLAT" >/dev/null 2>&1 || true
git -C "$METRICS_REPO" diff --cached --name-only 2>/dev/null | while IFS= read -r f; do
  [ -f "$METRICS_REPO/$f" ] || continue
  mb=$(( $(wc -c <"$METRICS_REPO/$f") / 1048576 ))
  if [ "$mb" -gt "${MAX_PUSH_FILE_MB:-25}" ]; then
    git -C "$METRICS_REPO" reset -q -- "$f"
    log "WARN: not pushing oversized file ($mb MB > ${MAX_PUSH_FILE_MB:-25} MB): $f"
  fi
done
CHANGED_SUMMARY="$(IFS=,; echo "${CHANGED_BR[*]}")"
if git -C "$METRICS_REPO" commit -q -m "regression $PLAT $DATE [$CHANGED_SUMMARY]" >/dev/null 2>&1; then
  pushed=0
  for attempt in 1 2 3; do
    git -C "$METRICS_REPO" pull --rebase --autostash -q >/dev/null 2>&1 || true
    if git -C "$METRICS_REPO" push -q >/dev/null 2>&1; then pushed=1; break; fi
    log "push attempt $attempt failed (concurrent update?); rebasing + retrying."
  done
  [ "$pushed" = "1" ] && log "pushed results to metrics." \
    || log "WARN: push failed after 3 attempts; results not persisted (re-measures next run)."
else
  log "nothing new to commit."
fi
fi   # end publish guard

# ── Alert email: gate + test detail, addressed from the mbirtorch-nightly identity ────────────
if [ "$GATE_FAIL" != "0" ] || [ "$TEST_FAIL" != "0" ] || [ -n "$LEDGER_ALERT" ]; then
  SM="$(command -v sendmail || echo /usr/sbin/sendmail)"
  if [ -x "$SM" ]; then
    _brs="$(IFS=,; echo "${CHANGED_BR[*]}")"
    { printf 'Subject: [mbirtorch-nightly] %s regression: %s\nTo: %s\n\n' "$PLAT" "${_brs:-<none>}" "$MAIL_TO"
      echo "mbirtorch nightly ($PLAT) on $(hostname) at $(date '+%F %T')."
      [ -n "$TORCHWATCH" ] && { echo; echo "$TORCHWATCH"; [ -n "$HEADROOM" ] && echo "$HEADROOM"; echo; }
      echo "Branches measured this run: ${_brs:-<none>}"
      echo
      [ -n "$LEDGER_ALERT" ] && { echo "dep-canary: $LEDGER_ALERT"; echo; }
      cat "$ALERT_BODY"
      [ -n "${SLURM_JOB_ID:-}" ] && echo "Full log: $WORK_DIR/nightly-${SLURM_JOB_ID}.log"
      echo "Records:  $RES/<branch>/  (record book records_${PLAT}.yaml)"
      echo
      echo "Note: a hard-gate regression AUTO-ADVANCES the baseline, so an alert fires ONCE per"
      echo "regressing change — review and revert if it is not an expected/accepted change.  Test"
      echo "failures are non-fatal (they do not change the exit code)."
    } | "$SM" -t && log "notify email sent to $MAIL_TO." || log "WARN: notify email send failed (non-fatal)."
  else
    log "WARN: no sendmail found — notify email skipped (slurm --mail-type=FAIL still covers gate fails)."
  fi
fi
rm -f "$ALERT_BODY"

[ "$GATE_FAIL" = "0" ] || { log "REGRESSION DETECTED — exit 1 (alert)."; exit 1; }
[ "$TEST_FAIL" = "0" ] && log "done — no regressions." \
  || log "done — no gate regression, but test failures/aborts occurred (emailed, non-fatal)."
exit 0
