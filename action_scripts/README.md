# action_scripts

These are the top-level entry points and the run knobs for this metrics repo.  Each script is a thin
wrapper around the engine or the harness in `tooling/`.  Each keeps the terminal open on a nonzero
exit instead of closing it.

| script | purpose |
|---|---|
| `build_dashboard.sh` | Rebuild the static dashboard (`dashboard/index.html`) from the YAML time series and open it locally.  The live site is rebuilt automatically by a GitHub Action; see the repo README.  Wraps `tooling/dashboard/build_dashboard.py`. |
| `add_run.sh` | Measure a **specific mbirtorch commit** and add it to the tracked time series, for example to seed an older commit onto the timeline.  See the section below. |
| `run_one_night.sh` | Run **one nightly pass** by hand.  This is the faithful single invocation of `tooling/regression/run_regression.sh`: for each tracked branch whose remote tip moved, clone it, run the tests and the engine, write results, and push.  Use it to verify the pipeline before enabling the schedule.  On a SLURM cluster, add `--sbatch` to submit it as a batch job on a GPU node. |
| `enable_nightly.sh` / `disable_nightly.sh` | Start and stop the **scheduled** nightly.  Platform-aware: macOS uses a launchd agent, and Gautschi uses a managed SLURM `scrontab` block with resources from `run_configs.env`. |
| `status_nightly.sh` | **Is the nightly on, and what has it done?**  Read-only check of both layers that must hold, which are the schedule and the `ENABLED` kill-switch, then the last wake time, a table of recent runs, and a summary of unacknowledged correctness divergences. |
| `clear_correctness.sh` | **Acknowledge reviewed correctness divergences** through a date.  Writes `results/correctness_acks.yaml`, so those divergences drop off the dashboard banner and the browser-tab badge, with the record kept.  With no arguments it prints the status and asks to clear through today. |
| `create_token.sh` | One-time setup of the fine-grained GitHub token used for the unattended push.  See `create_token_instructions.md`. |

## `add_run.sh` in detail

`add_run.sh --local` measures the **committed `HEAD`** of the branch in your current mbirtorch
checkout.  Uncommitted changes to tracked files are rejected, because the run measures the commit
and not your live working tree.  `add_run.sh <ref>` measures a branch, tag, or sha from the
mbirtorch repo at `MBIRTORCH_REPO`, which defaults to `../mbirtorch`.  With no arguments it prints
help.

Either way it checks out that commit into a **throwaway git worktree**, so your tree is untouched,
and measures it through the **same pipeline as the nightly**.  That pipeline is the dedicated
`mbirtorch_regression` env with the worktree pip-installed editable, shared through
`tooling/regression/lib_env.sh`.  A seeded point is therefore comparable to the nightly runs around
it.  Your development env is never touched.

Installing the worktree, rather than only setting `PYTHONPATH`, is required.  A modern editable
install registers a `sys.meta_path` finder that takes precedence over `PYTHONPATH`, so without the
install the engine would silently measure whatever mbirtorch is already in the env.

On a SLURM cluster, append `--sbatch` to submit the run as a batch job on a GPU node instead of
running it in this session.

## Run knobs — `run_configs.env`

These are the knobs you edit.  The harness sources this file through
`tooling/regression/regression.env`.  Each run pulls the metrics repo before measuring, so edits
here propagate to the nightly automatically.

| knob | scope | what it sets |
|---|---|---|
| `TRACKED_BRANCHES` | all | mbirtorch branches to watch.  Each is measured only when its remote tip moves. |
| `TORCH_INDEX_URL_gpu` / `TORCH_INDEX_URL_cpu` | all | the wheel index each platform installs torch from.  torch selects its CUDA build through the index, not through a pip extra, so the GPU value must stay in sync with the CUDA module the node loads. |
| `INSTALL_EXTRAS` | all | pip extras for each branch's editable install (`test` = pytest and pytest-xdist). |
| `CONDA_PYTHON` | all | Python version for the dedicated `mbirtorch_regression` env, used only when the harness has to create it. |
| `MEM_GATE_WINDOW` | all | rolling-minimum window in runs for the GPU memory gate.  1 is a single-shot compare, which is correct here; see the comment in the file for the measurement behind that. |
| `DEVICE_COUNTS` | all | device counts the nightly sweeps.  Counts above 1 apply only at the engine's multi-device cells. |
| `MACOS_NIGHTLY_TIME` | macOS | local 24-hour `HH:MM` the launchd nightly runs.  Pick a time the Mac is **awake**; a scheduled wake from sleep is a dark wake and will not fire a LaunchAgent.  Re-run `enable_nightly.sh` after changing it. |
| `SLURM_ACCOUNT` · `SLURM_PARTITION` · `SLURM_QOS` | cluster | SLURM account, partition (`ai`, H100), and QoS (`normal`; `standby` is not accepted on `ai`). |
| `SLURM_GPUS_PER_NODE` | cluster | GPUs for the sweep.  The n=2 and n=4 rows need four. |
| `SLURM_NTASKS` | cluster | CPU cores.  Fourteen per GPU is the required ratio on `ai`. |
| `SLURM_WALLTIME` | cluster | walltime ceiling.  Fire-on-change exits in seconds on a no-change night, so this is only a cap. |

Harness *infrastructure* lives in `tooling/regression/regression.env`, not here.  That covers URLs,
paths, credentials, the schedule cadence, and the `ENABLED` kill-switch.

## One-time setup

The nightly runs on a **Mac**, through launchd, for the CPU sweep, and on **Gautschi**, through a
SLURM `scrontab` entry, for the GPU sweep.  They write disjoint paths, so both can track the same
branches in parallel.  Set up each machine once.  In both cases the dedicated
`mbirtorch_regression` conda env is created on the first run, and the standing checkout is only the
entry point; each run clones its own working copy under `WORK_DIR`.

### macOS (CPU)

From a shell where `conda` is on your PATH:

1. **Clone** the metrics repo to a stable location:
   ```
   git clone https://github.com/cabouman/mbirtorch_metrics ~/mbirtorch_metrics && cd ~/mbirtorch_metrics
   ```
   The launchd agent does not run from this checkout.  It runs from an entry clone under
   `~/.mbirtorch/entry`, which `enable_nightly.sh` creates and refreshes, because a launchd process
   cannot read anything under `~/Documents`.
2. **Push token (optional on macOS)** — git can push through your macOS keychain.  For a scoped
   token instead, run `action_scripts/create_token.sh`.
3. **Tune** `run_configs.env`.
4. **Smoke-test, then schedule:**
   ```
   REG_SMOKE=1 bash tooling/regression/run_regression.sh   # a plumbing check, no push
   action_scripts/run_one_night.sh                          # one real pass (measures and pushes)
   action_scripts/enable_nightly.sh                         # load the launchd agent
   action_scripts/status_nightly.sh                         # confirm it is on
   ```
   `disable_nightly.sh` unloads it.  Logs land in `~/.mbirtorch/regression/launchd.{out,err}.log`.

### Cluster (Gautschi, GPU)

On a Gautschi login node:

1. **Clone** the metrics repo to a stable location:
   ```
   git clone https://github.com/cabouman/mbirtorch_metrics ~/mbirtorch_metrics && cd ~/mbirtorch_metrics
   ```
2. **Preamble** — copy the template to the path `regression.env` expects.  It loads the conda and
   cuda modules and exports the proxy the compute nodes need to reach GitHub:
   ```
   cp tooling/regression/cluster_preamble.sh.example "$HOME/load_conda_cuda.sh"
   ```
3. **Push token (required)** — a compute node has no keychain:
   ```
   action_scripts/create_token.sh        # writes ~/.config/mbirtorch/metrics_credentials (chmod 600)
   ```
   The token must grant write access to `cabouman/mbirtorch_metrics`.
4. **Tune** the `SLURM_*` knobs in `run_configs.env` if needed.
5. **Smoke-test, then schedule** from an interactive GPU session
   (`sinteractive -A bouman -N1 -n56 --gpus-per-node=4 -p ai -t 2:00:00`):
   ```
   REG_SMOKE=1 bash tooling/regression/run_regression.sh   # a plumbing check, no push
   action_scripts/run_one_night.sh                          # one real pass (measures and pushes)
   action_scripts/enable_nightly.sh                         # install the scrontab schedule
   action_scripts/status_nightly.sh                         # confirm it is on
   ```
   `disable_nightly.sh` removes it.  To pre-flight the SLURM directives without running, use
   `sbatch --test-only tooling/regression/nightly_regression.slurm`.

See `tooling/dashboard/README.md` for the dashboard and `tooling/regression/README.md` for the
nightly.
