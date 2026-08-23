#!/usr/bin/env bash
# sbatch_submit.sh — shared "(re)submit this run as a SLURM batch job" mechanism, used by the --sbatch
# flag on action_scripts/add_run.sh and run_one_night.sh.  Lets you queue a measurement on a compute
# node instead of tying up an interactive session: `add_run.sh <ref> --sbatch`, `run_one_night.sh --sbatch`.
#
# Requires the cluster knobs to be in the environment (source regression.env first, which sources
# run_configs.env): SLURM_ACCOUNT / SLURM_PARTITION / SLURM_QOS / SLURM_GPUS_PER_NODE / SLURM_NTASKS /
# SLURM_WALLTIME, plus PREAMBLE_FILE (module load conda+cuda) and NOTIFY (mail).  The directive mapping
# mirrors enable_nightly.sh's cluster path, so a batch run matches the scheduled nightly's resources.
#
#   submit_sbatch <job-name> <command> [args...]
#
# The batch job sources the node preamble (so `import mbirtorch`'s conda/cuda is available) and execs the
# command.  The slurm log lands in the submit directory as <job-name>-<jobid>.log.  Returns sbatch's exit
# code (and 2 if sbatch isn't available — i.e. this isn't a SLURM cluster).
submit_sbatch() {
  local jobname="$1"; shift
  if ! command -v sbatch >/dev/null 2>&1; then
    echo "--sbatch: 'sbatch' not found — this is not a SLURM cluster, so run without --sbatch." >&2
    return 2
  fi
  local opts="-A ${SLURM_ACCOUNT} -p ${SLURM_PARTITION} -q ${SLURM_QOS} -N1"
  opts+=" --gpus-per-node=${SLURM_GPUS_PER_NODE} -n ${SLURM_NTASKS} -t ${SLURM_WALLTIME}"
  opts+=" -J ${jobname} -o ${jobname}-%j.log"
  [ -n "${NOTIFY:-}" ] && opts+=" --mail-user=${NOTIFY} --mail-type=FAIL"
  local pre=""
  [ -n "${PREAMBLE_FILE:-}" ] && [ -f "${PREAMBLE_FILE:-}" ] && printf -v pre 'source %q\n' "$PREAMBLE_FILE"
  local cmd; printf -v cmd '%q ' "$@"
  echo "submitting SLURM batch job '${jobname}': ${SLURM_PARTITION}/${SLURM_QOS}, ${SLURM_GPUS_PER_NODE} GPU(s), t=${SLURM_WALLTIME}"
  echo "  runs: $*"
  # $opts is intentionally word-split into separate flags.
  # shellcheck disable=SC2086
  sbatch $opts <<EOF
#!/bin/bash
${pre}exec ${cmd}
EOF
}
