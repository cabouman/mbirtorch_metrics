#!/usr/bin/env bash
# enable_probe.sh — install + start the weekly cluster probe (cluster_probe.sh) as a managed
# scrontab block (`# mbirtorch-probe-BEGIN/END`), the way enable_nightly.sh installs the nightly.
# It manages ONLY that block.  Cluster only.  Re-run after editing the PROBE_* knobs.
#     PROBE_SCHEDULE="*/5 * * * *" ./enable_probe.sh     # one-off override, e.g. a live test firing
set -euo pipefail
if [ -t 0 ]; then
  trap '_ec=$?; [ "$_ec" -ne 0 ] && { echo; echo ">>> $(basename "$0") exited with status $_ec — press Enter to close."; read -r _ || true; }' EXIT
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/regression.env"
# shellcheck disable=SC1091
source "$HERE/lib_scron.sh"
[ "$(uname -s)" != "Darwin" ] || { echo "enable_probe: the probe runs on the cluster only; nothing to do on macOS."; exit 0; }
command -v scrontab >/dev/null 2>&1 || { echo "ERROR: scrontab not found — this cluster's Slurm lacks the cron feature."; exit 1; }
PROBE="$HERE/cluster_probe.sh"
[ -f "$PROBE" ] || { echo "ERROR: probe not found at $PROBE"; exit 1; }
PROBE_SCHEDULE="${PROBE_SCHEDULE:-0 8 * * 1}"       # Monday 08:00, after the 03:00 nightly can finish
PROBE_WALLTIME="${PROBE_WALLTIME:-0:15:00}"
WORK_DIR="${WORK_DIR:-$HOME/.mbirtorch/regression}"; mkdir -p "$WORK_DIR"
OPTS="-A ${SLURM_ACCOUNT:-bouman} -p ${SLURM_PARTITION:-ai} -q ${SLURM_QOS:-normal} -N1 --gpus-per-node=1 -n 14"
OPTS="$OPTS -t ${PROBE_WALLTIME} -J mbirtorch-probe --mail-user=${NOTIFY} --mail-type=FAIL,TIME_LIMIT -o ${WORK_DIR}/probe-%j.log"
scron_block_install mbirtorch-probe "$OPTS" "$PROBE_SCHEDULE" "bash $PROBE"
echo "Installed scrontab mbirtorch-probe:"
echo "  schedule: $PROBE_SCHEDULE   account: ${SLURM_ACCOUNT:-bouman}   ${SLURM_PARTITION:-ai}/${SLURM_QOS:-normal}   1 GPU   t=$PROBE_WALLTIME"
echo "  probe:    $PROBE"
echo "  logs:     $WORK_DIR/probe-<jobid>.log"
echo "  status:   ${PROBE_STATUS_DIR:-/depot/bouman/data/cluster_status}/probe_status.txt  (mail to ${NOTIFY} every run)"
echo "  inspect:  scrontab -l   |   squeue --me   |   ./status_nightly.sh"
