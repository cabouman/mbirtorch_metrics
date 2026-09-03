#!/usr/bin/env bash
# cluster_probe.sh — the weekly cluster probe.  Does the cluster underneath the nightly still look
# the way the group's tooling assumes?  Runs as a scron job (enable_probe.sh) on ONE GPU: gathers
# facts, compares them with the previous run and with thresholds, writes both to a group-readable
# depot directory, mails the report EVERY run (a missing Monday mail is itself the alarm), and
# exits 1 on any finding so Slurm's own FAIL mail backs the report up.
#
# Design record: mbirtorch_plans/plans/mbirtorch_metrics/cluster_probe_plan.md.  The rules:
#   * every fact is tri-state — key=value, or key=UNKNOWN:<reason>; every UNKNOWN is a finding,
#     and every threshold test PASSES ONLY IF the value parses and satisfies the rule, so a check
#     that errors can never read as a pass;
#   * identity facts (driver, partition limits, env versions) are diffed against the previous
#     run's facts file, which then advances — one mail per change, no committed baseline;
#   * nothing here changes the cluster: it reads, computes, and writes its own output files;
#   * a scron job has a clean environment and $HOME as its cwd (Slurm ignores the user's
#     environment), so everything below is sourced or resolved by absolute path.
# Knobs: the PROBE_* variables in regression.env (defaults below).  Any can be overridden for one
# run:   sbatch --export=PROBE_MAIL=0,PROBE_HOME_PCT_MAX=1 [slurm opts] --wrap "bash <this file>"
set -o pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log() { echo "[$(date '+%F %T')] $*"; }

# ── environment: login-shell setup, the harness knobs, the node preamble ─────────────────────
# /etc/profile reads unset variables, so it (and the files it pulls in) run before `set -u`.
[ -r /etc/profile ] && . /etc/profile >/dev/null 2>&1
# shellcheck disable=SC1091
[ -f "$HERE/regression.env" ] && source "$HERE/regression.env"
PROBE_STATUS_DIR="${PROBE_STATUS_DIR:-/depot/bouman/data/cluster_status}"
PROBE_FALLBACK_DIR="${PROBE_FALLBACK_DIR:-$HOME/.mbirtorch/probe}"
PROBE_ENVS="${PROBE_ENVS:-mbirtorch pcdrecon mbirtorch_regression}"
PROBE_MAIL="${PROBE_MAIL:-1}"
PROBE_HOME_PCT_MAX="${PROBE_HOME_PCT_MAX:-80}"
PROBE_SCRATCH_FILES_PCT_MAX="${PROBE_SCRATCH_FILES_PCT_MAX:-80}"
PROBE_DEPOT_PCT_MAX="${PROBE_DEPOT_PCT_MAX:-90}"
PROBE_BALANCE_MIN="${PROBE_BALANCE_MIN:-2000}"
PROBE_NIGHTLY_MAX_AGE_H="${PROBE_NIGHTLY_MAX_AGE_H:-48}"
PROBE_WWW_BASE="${PROBE_WWW_BASE:-/depot/bouman/www}"
PROBE_WWW_ROOTS="${PROBE_WWW_ROOTS:-$PROBE_WWW_BASE/mbirtorch $PROBE_WWW_BASE/mbirjax $PROBE_WWW_BASE/pcdrecon}"
PROBE_CLUSTER="${PROBE_CLUSTER:-gautschi}"
WORK_DIR="${WORK_DIR:-$HOME/.mbirtorch/regression}"
NOTIFY="${NOTIFY:-$USER@purdue.edu}"
PREAMBLE_FILE="${PREAMBLE_FILE:-$HOME/load_conda_cuda.sh}"
if [ -f "$PREAMBLE_FILE" ]; then
  # shellcheck disable=SC1090
  source "$PREAMBLE_FILE" >/dev/null 2>&1 || log "WARN: sourcing $PREAMBLE_FILE returned nonzero"
fi
set -u

# ── the fact store, findings, and the pass-only-if tests ─────────────────────────────────────
declare -A F; KEYS=(); FINDINGS=(); NOTES=()
fact()    { F["$1"]="$2"; KEYS+=("$1"); }
unknown() { fact "$1" "UNKNOWN:$2"; FINDINGS+=("$1 is UNKNOWN ($2)"); }
finding() { FINDINGS+=("$1"); }
note()    { NOTES+=("$1"); }
is_num()  { [[ "${1:-}" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; }
lt()      { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a+0 <  b+0)}'; }
gt()      { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a+0 >  b+0)}'; }
# A test passes only when the value is numeric AND satisfies the rule; UNKNOWN was already counted.
check_max() { local v="${F[$1]:-}"; case "$v" in UNKNOWN:*) return;; esac; { is_num "$v" && lt "$v" "$2"; } || finding "$1=$v (must be < $2)"; }
check_min() { local v="${F[$1]:-}"; case "$v" in UNKNOWN:*) return;; esac; { is_num "$v" && gt "$v" "$2"; } || finding "$1=$v (must be > $2)"; }
check_eq()  { local v="${F[$1]:-}"; case "$v" in UNKNOWN:*) return;; esac; [ "$v" = "$2" ] || finding "$1=$v (must be $2)"; }
T() { timeout -k 5 "$@"; }                      # every external command runs under a timeout
# fcount: count find(1) hits, or print UNKNOWN when find fails or times out (a 0 from a failed
# find would be a false pass).
fcount() { local tmp; tmp="$(mktemp)"; if T 180 find "$@" >"$tmp" 2>/dev/null; then wc -l <"$tmp" | tr -d ' '; else echo UNKNOWN; fi; rm -f "$tmp"; }
count_fact() { local k="$1"; shift; local v; v="$(fcount "$@")"; [ "$v" = UNKNOWN ] && unknown "$k" "find failed or timed out" || fact "$k" "$v"; }

# ── never a silent death: if the verdict is not reached, say so and exit 1 ───────────────────
DONE=0; VERDICT="UNKNOWN"; OUT="$PROBE_FALLBACK_DIR"
send_mail() {   # $1=subject verdict, stdin=body
  [ "$PROBE_MAIL" = "1" ] || { log "PROBE_MAIL=$PROBE_MAIL — mail skipped."; cat >/dev/null; return 0; }
  local SM; SM="$(command -v sendmail || echo /usr/sbin/sendmail)"
  [ -x "$SM" ] || { log "WARN: no sendmail — mail skipped (slurm --mail-type=FAIL still covers exit 1)."; cat >/dev/null; return 0; }
  { printf 'Subject: [cluster-probe] %s %s %s\nTo: %s\n\n' "$1" "$PROBE_CLUSTER" "$(date +%F)" "$NOTIFY"; cat; } | "$SM" -t \
    && log "mail sent to $NOTIFY." || log "WARN: mail send failed (non-fatal)."
}
on_exit() {
  local rc=$?
  if [ "$DONE" != "1" ]; then
    log "probe died before reaching a verdict (rc=$rc) — reporting UNKNOWN."
    mkdir -p "$PROBE_FALLBACK_DIR" 2>/dev/null
    printf '%s UNKNOWN probe-died rc=%s node=%s job=%s\n' "$(date -Is)" "$rc" "$(hostname -s)" "${SLURM_JOB_ID:-none}" \
      | tee "$PROBE_FALLBACK_DIR/probe_status.txt" > "$PROBE_STATUS_DIR/probe_status.txt" 2>/dev/null || true
    printf 'The cluster probe died before reaching a verdict (rc=%s) on %s, job %s.\nLog: %s/probe-%s.log\n' \
      "$rc" "$(hostname -s)" "${SLURM_JOB_ID:-none}" "$WORK_DIR" "${SLURM_JOB_ID:-none}" | send_mail "UNKNOWN"
    exit 1
  fi
}
trap on_exit EXIT

log "cluster probe starting on $(hostname -s), job ${SLURM_JOB_ID:-none}, status dir $PROBE_STATUS_DIR"
fact probe.version 1
fact date "$(date -Is)"
fact node "$(hostname -s)"
fact jobid "${SLURM_JOB_ID:-none}"
fact log "$(T 30 scontrol show job "${SLURM_JOB_ID:-0}" 2>/dev/null | grep -oE 'StdOut=[^ ]+' | head -1 | cut -d= -f2)"
fact slurm "$(T 30 sinfo --version 2>/dev/null | awk '{print $2}')"

# ── driver and CUDA ceiling (identity) ───────────────────────────────────────────────────────
drv="$(T 60 nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')"
[ -n "$drv" ] && fact driver "$drv" || unknown driver "nvidia-smi query failed (no GPU on this node?)"
cmax="$(T 60 nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | head -1 | awk '{print $3}')"
[ -n "$cmax" ] && fact cuda_max "$cmax" || unknown cuda_max "nvidia-smi banner missing"

# ── partition limits (identity) ──────────────────────────────────────────────────────────────
pinfo="$(T 60 scontrol show partition ai 2>/dev/null)"
for k in DefaultTime MaxTime DefMemPerCPU MaxMemPerCPU; do
  v="$(printf '%s' "$pinfo" | grep -oE "$k=[^ ]+" | head -1 | cut -d= -f2)"
  [ -n "$v" ] && fact "partition.ai.$k" "$v" || unknown "partition.ai.$k" "scontrol show partition ai"
done

# ── modules as the preamble leaves them (informational: verified 2026-09-03 not to matter) ────
ml="$(module list 2>&1 || true)"
for m in conda cuda cudnn; do
  v="$(printf '%s' "$ml" | grep -oE "(^|[ )])$m/[0-9A-Za-z.-]+" | head -1 | sed "s#.*$m/##")"
  fact "module.$m" "${v:-none}"
done
fact cuda_default "$(module avail cuda 2>&1 | grep -oE 'cuda/[0-9.]+ *\((L,)?D\)' | grep -oE '[0-9.]+' | head -1)"

# ── the node preamble matches the repo example, comments and blank lines aside ───────────────
strip_md5() { sed 's/#.*//; s/[[:space:]]*$//; /^[[:space:]]*$/d' "$1" | md5sum | cut -d' ' -f1; }
if [ -f "$PREAMBLE_FILE" ] && [ -f "$HERE/cluster_preamble.sh.example" ]; then
  [ "$(strip_md5 "$PREAMBLE_FILE")" = "$(strip_md5 "$HERE/cluster_preamble.sh.example")" ] && fact preamble.ok 1 || fact preamble.ok 0
else
  unknown preamble.ok "missing $PREAMBLE_FILE or $HERE/cluster_preamble.sh.example"
fi
check_eq preamble.ok 1

# ── each env: versions (identity) and a real CUDA workload (gpu_ok) ──────────────────────────
for e in $PROBE_ENVS; do
  py="$HOME/.conda/envs/$e/bin/python"
  if [ ! -x "$py" ]; then
    for kv in python torch torch_cuda triton; do fact "env.$e.$kv" "UNKNOWN:no env"; done
    unknown "env.$e.gpu_ok" "no python at $py"; continue
  fi
  out="$(T 300 "$py" - <<'PY' 2>&1
import sys
try:
    import torch
    try:
        import triton; tv = triton.__version__
    except Exception:
        tv = "none"
    assert torch.cuda.is_available(), "torch.cuda.is_available() is False"
    a = torch.randn(256, 256, device="cuda"); s = float((a @ a).sum())
    assert s == s, "GPU matmul produced NaN"
    print(f"PROBE_ENV python={sys.version.split()[0]} torch={torch.__version__} "
          f"torch_cuda={torch.version.cuda} triton={tv} dev={torch.cuda.get_device_name(0).replace(' ', '_')}")
except Exception as exc:
    print(f"PROBE_ERR {type(exc).__name__}: {exc}")
    sys.exit(1)
PY
)"
  line="$(printf '%s\n' "$out" | grep '^PROBE_ENV ' | tail -1)"
  if [ -n "$line" ]; then
    for kv in python torch torch_cuda triton; do fact "env.$e.$kv" "$(printf '%s' "$line" | grep -oE " $kv=[^ ]+" | cut -d= -f2)"; done
    fact "env.$e.gpu_ok" 1
  else
    for kv in python torch torch_cuda triton; do fact "env.$e.$kv" "UNKNOWN:probe failed"; done
    fact "env.$e.gpu_ok" 0
    finding "env.$e.gpu_ok=0 ($(printf '%s\n' "$out" | grep -E 'PROBE_ERR|Error|error' | tail -1 | cut -c1-160))"
  fi
done

# ── hollow envs: a directory under ~/.conda/envs that is no longer an env (scratch purge) ─────
hollow=""
for d in "$HOME"/.conda/envs/*/; do
  [ -d "$d" ] || continue
  { [ -x "$d/bin/python" ] && [ -f "$d/conda-meta/history" ]; } || hollow="$hollow $(basename "$d")"
done
fact hollow_envs "$(echo $hollow | wc -w | tr -d ' ')"
[ -z "$hollow" ] || finding "hollow_envs:$hollow (purged or broken — rm -rf and rebuild with clean_install_all.sh)"

# ── quotas and balance ───────────────────────────────────────────────────────────────────────
# myquota fetches its table from the cluster's internal aux server over HTTPS; the preamble's squid
# proxy variables would route that request through the proxy, which cannot reach it (trial 1,
# 2026-09-03: all three quota rows came back empty).  So it runs with the proxy unset.
mq="$(T 60 env -u HTTPS_PROXY -u HTTP_PROXY -u https_proxy -u http_proxy myquota 2>&1 || true)"
printf '%s\n' "$mq" | grep -q '^home' || log "myquota gave no home row; its output was: $(printf '%s' "$mq" | head -3 | tr '\n' '|')"
hp="$(printf '%s\n' "$mq" | awk '$1=="home"{print $5}' | tr -d '%')"
[ -n "$hp" ] && fact home_pct "$hp" || unknown home_pct "myquota home row"
fact home_used "$(printf '%s\n' "$mq" | awk '$1=="home"{print $3"/"$4}')"
sp="$(printf '%s\n' "$mq" | awk '$1=="scratch"{print $5}' | tr -d '%')"; fact scratch_pct "${sp:-UNKNOWN:myquota}"
sfp="$(printf '%s\n' "$mq" | awk '$1=="scratch"{print $8}' | tr -d '%')"
[ -n "$sfp" ] && fact scratch_files_pct "$sfp" || unknown scratch_files_pct "myquota scratch row"
dp="$(printf '%s\n' "$mq" | awk '$1=="depot" && $2=="bouman"{print $5}' | tr -d '%')"
[ -n "$dp" ] && fact depot_pct "$dp" || unknown depot_pct "myquota depot bouman row"
check_max home_pct "$PROBE_HOME_PCT_MAX"
check_max scratch_files_pct "$PROBE_SCRATCH_FILES_PCT_MAX"
check_max depot_pct "$PROBE_DEPOT_PCT_MAX"
bal="$(T 60 slist 2>/dev/null | awk '$1=="bouman"{print $NF}' | tr -d ',')"
is_num "$bal" && fact slist_balance "$bal" || unknown slist_balance "slist bouman row"
check_min slist_balance "$PROBE_BALANCE_MIN"

# ── scrontab: nothing disabled, the nightly still installed (raw listing goes to the log only) ─
sc="$(T 60 scrontab -l 2>/dev/null || true)"
if [ -n "$sc" ]; then
  fact scrontab.disabled "$(printf '%s\n' "$sc" | grep -c '^#DISABLED' || true)"
  printf '%s\n' "$sc" | grep -qF '# mbirtorch-nightly-BEGIN' && fact scrontab.nightly 1 || fact scrontab.nightly 0
  echo "--- scrontab -l ---"; printf '%s\n' "$sc"; echo "--- end scrontab ---"
else
  unknown scrontab.disabled "scrontab -l empty or failed"; unknown scrontab.nightly "scrontab -l empty or failed"
fi
check_eq scrontab.disabled 0
check_eq scrontab.nightly 1

# ── the nightly: alive, finished cleanly, and pushed (skipped while it is running) ───────────
running="$(T 60 squeue --me --name=mbirtorch-nightly -t RUNNING -h 2>/dev/null | wc -l | tr -d ' ')"
fact nightly.running "$running"
nlog="$(ls -t "$WORK_DIR"/nightly-*.log 2>/dev/null | head -1 || true)"
if [ -n "$nlog" ] && [ -f "$nlog" ]; then
  fact nightly.log_age_h "$(( ( $(date +%s) - $(stat -c %Y "$nlog") ) / 3600 ))"
  last="$(tail -1 "$nlog" | cut -c1-140)"
  fact nightly.last_line "$last"
  printf '%s' "$last" | grep -qE 'done\.|done — |REGRESSION DETECTED|FATAL|ENABLED=0' && fact nightly.log_complete 1 || fact nightly.log_complete 0
else
  unknown nightly.log_age_h "no $WORK_DIR/nightly-*.log"; unknown nightly.log_complete "no nightly log"
fi
if [ -d "$WORK_DIR/metrics/.git" ]; then
  u="$(T 60 git -C "$WORK_DIR/metrics" rev-list --count '@{u}..HEAD' 2>/dev/null || true)"
  is_num "$u" && fact nightly.unpushed "$u" || unknown nightly.unpushed "git rev-list in $WORK_DIR/metrics"
else
  unknown nightly.unpushed "no clone at $WORK_DIR/metrics"
fi
if [ "$running" = "0" ]; then
  check_max nightly.log_age_h "$PROBE_NIGHTLY_MAX_AGE_H"
  check_eq nightly.log_complete 1
  check_eq nightly.unpushed 0
else
  note "the nightly is RUNNING now — its log-age / completeness / unpushed checks were skipped"
fi

# ── the public web root: readable, nothing world-writable, no data or source, no escaping links ─
unread=0; dang=0; syml=0; esc=0; wwwbad=0
for root in $PROBE_WWW_ROOTS; do
  [ -d "$root" ] || { note "www root missing: $root"; continue; }
  v="$(fcount "$root" \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=rx \))"; [ "$v" = UNKNOWN ] && wwwbad=1 || unread=$(( unread + v ))
  v="$(fcount "$root" \( -path '*/_static' -o -path '*/_sources' -o -path '*/_downloads' \) -prune -o -type f \( -iname '*.npy' -o -iname '*.npz' -o -iname '*.h5' -o -iname '*.hdf5' -o -iname '*.tgz' -o -iname '*.tar' -o -iname '*.gz' -o -iname '*.zip' -o -iname '*.pkl' -o -iname '*.pt' -o -iname '*.pth' -o -iname '*.env' -o -iname '*.pem' -o -iname '*.key' -o -iname '*.py' -o -iname '*.sh' -o -iname '*.ipynb' \) -print)"
  [ "$v" = UNKNOWN ] && wwwbad=1 || dang=$(( dang + v ))
  while IFS= read -r l; do
    [ -n "$l" ] || continue; syml=$(( syml + 1 ))
    tgt="$(readlink -f "$l" 2>/dev/null || true)"
    case "$tgt" in "$PROBE_WWW_BASE"/*) ;; *) esc=$(( esc + 1 ));; esac
  done < <(T 180 find "$root" -type l 2>/dev/null || true)
done
if [ "$wwwbad" = "1" ]; then unknown www.unreadable "find failed or timed out"; unknown www.dangerous "find failed or timed out"
else fact www.unreadable "$unread"; fact www.dangerous "$dang"; fi
fact www.symlinks "$syml"; fact www.escaping_symlinks "$esc"
count_fact www.world_writable "$PROBE_WWW_BASE" ! -type l -perm -o=w
check_eq www.unreadable 0
check_eq www.dangerous 0
check_eq www.world_writable 0
check_eq www.escaping_symlinks 0

# ── where the output goes: depot if writable, else home (and that is a finding) ──────────────
if mkdir -p "$PROBE_STATUS_DIR" 2>/dev/null && ( : > "$PROBE_STATUS_DIR/.write_test" ) 2>/dev/null; then
  rm -f "$PROBE_STATUS_DIR/.write_test"; fact depot_writable 1; OUT="$PROBE_STATUS_DIR"
else
  fact depot_writable 0; OUT="$PROBE_FALLBACK_DIR"; mkdir -p "$OUT"
  finding "depot_writable=0 ($PROBE_STATUS_DIR not writable; output went to $OUT)"
fi

# ── identity diff against the previous run; the previous file then advances ──────────────────
IDENTITY="driver cuda_max partition.ai.DefaultTime partition.ai.MaxTime partition.ai.DefMemPerCPU partition.ai.MaxMemPerCPU"
for e in $PROBE_ENVS; do IDENTITY="$IDENTITY env.$e.python env.$e.torch env.$e.torch_cuda env.$e.triton"; done
PREV="$OUT/${PROBE_CLUSTER}_facts.txt"
if [ -f "$PREV" ]; then
  for k in $IDENTITY; do
    pv="$(grep -m1 "^$k=" "$PREV" | cut -d= -f2-)"; cv="${F[$k]:-}"
    case "$cv" in UNKNOWN:*) continue;; esac
    if [ -z "$pv" ]; then note "new fact: $k=$cv"
    elif [ "$pv" != "$cv" ]; then finding "changed: $k $pv -> $cv"; fi
  done
  pb="$(grep -m1 '^slist_balance=' "$PREV" | cut -d= -f2)"; pd="$(grep -m1 '^date=' "$PREV" | cut -d= -f2-)"
  if is_num "$pb" && is_num "${F[slist_balance]:-x}" && [ -n "$pd" ]; then
    days=$(( ( $(date +%s) - $(date -d "$pd" +%s 2>/dev/null || date +%s) ) / 86400 ))
    if [ "$days" -ge 1 ]; then
      fact burn_per_week "$(awk -v a="$pb" -v b="${F[slist_balance]}" -v d="$days" 'BEGIN{printf "%.1f", (a-b)/d*7}')"
      lt "${F[burn_per_week]}" 0 && note "balance rose since the previous run (top-up?): $pb -> ${F[slist_balance]}"
    else
      fact burn_per_week "n/a (previous run < 1 day ago)"
    fi
  fi
else
  note "first run: no previous facts file at $PREV — identity facts recorded, not compared"
fi

# ── verdict, files, report, mail, exit ───────────────────────────────────────────────────────
if [ "${#FINDINGS[@]}" -eq 0 ]; then VERDICT="PASS"; else VERDICT="FINDINGS(${#FINDINGS[@]})"; fi
fact probe.verdict "$VERDICT"
tmp="$(mktemp "$OUT/.facts.XXXXXX")"
for k in "${KEYS[@]}"; do printf '%s=%s\n' "$k" "${F[$k]}"; done > "$tmp"
[ -f "$PREV" ] && cp -f "$PREV" "$OUT/${PROBE_CLUSTER}_facts.prev.txt"
mv -f "$tmp" "$PREV"; chmod 644 "$PREV" 2>/dev/null
mkdir -p "$OUT/history"; cp -f "$PREV" "$OUT/history/facts-$(date +%F).txt"
REPORT="cluster probe: $VERDICT — $PROBE_CLUSTER $(date '+%F %T') on $(hostname -s), job ${SLURM_JOB_ID:-none}"
if [ "${#FINDINGS[@]}" -gt 0 ]; then REPORT="$REPORT"$'\n'"findings:"; for f in "${FINDINGS[@]}"; do REPORT="$REPORT"$'\n'"  - $f"; done
else REPORT="$REPORT"$'\n'"no findings."; fi
if [ "${#NOTES[@]}" -gt 0 ]; then REPORT="$REPORT"$'\n'"notes:"; for n in "${NOTES[@]}"; do REPORT="$REPORT"$'\n'"  - $n"; done; fi
REPORT="$REPORT"$'\n'"facts: $PREV"$'\n'"log:   ${F[log]:-$WORK_DIR/probe-${SLURM_JOB_ID:-none}.log}"
printf '%s\n' "$REPORT" > "$OUT/history/report-$(date +%F).txt"
printf '%s %s findings=%s node=%s job=%s\n' "$(date -Is)" "$VERDICT" "${#FINDINGS[@]}" "$(hostname -s)" "${SLURM_JOB_ID:-none}" > "$OUT/probe_status.txt"
echo; printf '%s\n' "$REPORT"; echo
{ printf '%s\n\n--- facts ---\n' "$REPORT"; cat "$PREV"; } | send_mail "$VERDICT"
DONE=1
[ "$VERDICT" = "PASS" ] && exit 0 || exit 1
