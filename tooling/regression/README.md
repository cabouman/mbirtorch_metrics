# Nightly regression harness

This is a standing, fire-on-change check on mbirtorch.  It watches a few branches.  Whenever one
moves, it measures every geometry, operator, size, and device count, recording minimum time, peak
memory, and a tolerant correctness fingerprint.  It then compares the run against that branch's own
previous run and flags regressions.  Cross-branch context and best-ever drift are shown on the
dashboard rather than gated here.

The harness runs in two places.  On a Mac it runs through launchd and measures the CPU series.  On
Purdue's Gautschi cluster it runs through a SLURM `scrontab` entry and measures the GPU series.
`enable_nightly.sh` and `disable_nightly.sh` install and remove both.

## Layout

The harness and the data live entirely in this repository, so edit them here directly.  There is no
deploy step.

```
action_scripts/          top-level entry points + run_configs.env (the run knobs); see its README
tooling/scaling_tests/   the engine: performance_tracking.py, scaling_common.py,
                         regression_to_table.py, test_gate.py, and the two manual entry points
tooling/regression/      this wrapper: run_regression.sh, regression.env, lib_env.sh,
                         lib_mac_entry.sh, enable/disable/status_nightly.sh, recent_runs.py,
                         com.mbirtorch.regression.plist (macOS), nightly_regression.slurm +
                         cluster_preamble.sh.example (cluster), sbatch_submit.sh, README.md
results/<plat>/<branch>/ regression_<plat>_<commit-time>_<sha8>.yaml (the time series), a sibling
                         _table.yaml (a browsable geometry/op/size/n view, written per run by the
                         engine), records_<plat>.yaml (best-ever), and tests_*.txt
state/<plat>/<branch>    the last MEASURED commit per branch, which is what fire-on-change reads
```

mbirtorch itself is only the library under test.  The nightly clones it fresh per changed branch and
never edits it.

## What a run does (`run_regression.sh`)

The wrapper has two phases and six steps.

1. **Bootstrap.**  Source the node preamble for the cluster proxy and modules, update or clone the
   persistent metrics clone at `$WORK_DIR/metrics`, and re-exec that copy.  Remote harness, config,
   and engine changes are therefore always picked up.
2. Activate the dedicated conda env and install the harness dependencies.
3. For each tracked branch, read its remote tip with `git ls-remote`, and skip it when the tip
   matches `state/`.
4. For each changed branch, shallow-clone the tip, install it into the dedicated env, run its test
   suite, and run the engine.
5. Commit and push `results/` and `state/`.  A push failure is not fatal; the next run retries it.
6. Exit non-zero only on a hard-gate regression, so the SLURM mail is a real alert.

The published dashboard rebuilds separately.  A GitHub Action regenerates it from the pushed YAML
and deploys it to Pages, so the nightly only needs to push data.

## Three guards specific to this harness

The platform key is declared by the wrapper and verified by the engine.  A GPU night on which CUDA
cannot initialise aborts loudly instead of filing itself under the CPU key.

Every measured row pins its device count and then asserts the realized device list.  An unpinned
mbirtorch model auto-widens on a multi-GPU node, so without the pin an all-device run would be filed
under a cell labelled n=1.  One unpinned settle per night, the `auto-choice` check, covers the
automatic path a user hits by default.

The engine refuses to measure under `MBIRTORCH_MEMORY_CALIBRATION`.  That mode resets and owns the
peak-memory counter, which is the ruler these rows read.

## One-time setup

1. Clone this repo somewhere stable.  That clone is the entry point.
2. Create the dedicated env, or let the harness create it on the first run:
   `conda create -n mbirtorch_regression python=3.12`.  Do not reuse a development env; the
   per-branch editable installs churn it.
3. Set the run knobs in `action_scripts/run_configs.env` and the infrastructure in `regression.env`.
   The nightly pulls the repo before each run, so committed and pushed edits propagate on their own.
4. For the unattended push, create a fine-grained personal access token with write access to
   `cabouman/mbirtorch_metrics` only, using `action_scripts/create_token.sh`.  Point `TOKEN_FILE` at
   the file it writes.

Then schedule it with `action_scripts/enable_nightly.sh`, or remove the schedule with
`disable_nightly.sh`.  On macOS these load and unload a launchd agent.  On the cluster they write
and remove a managed `scrontab` block.  The cluster needs two preparation steps first: copy the
preamble with `cp tooling/regression/cluster_preamble.sh.example "$HOME/load_conda_cuda.sh"`, and
create the push token.

## Is it on, and what has it done?

`tooling/regression/status_nightly.sh` is read-only.  It reports both layers that must hold for a
nightly to run: the schedule, and the `ENABLED` kill-switch in `regression.env`.  It then prints the
last wake time and a table of recent runs, followed by a summary of any unacknowledged correctness
divergences.  On the cluster it also shows any nightly currently in `squeue`.

The recent-runs table comes from `recent_runs.py`, which reuses the dashboard's own
`collect_data()`.  One parser serves both, so the table and the dashboard can never disagree about
what a run says.

## The torch-release watch and the dependency canary

The watch runs every night, including no-change nights.  It compares PyPI's latest torch with
`TORCH_LAST_REVIEWED` and prints two lines when a newer one exists: what shipped, and whether that
version can install on the regression env's Python.  Both lines also go into the notify email.  The
watch changes nothing on its own; it only says that a re-test is due.

The canary is the measuring half, and it is off by default (`DEP_CANARY_ENABLED=0`).  When it is on
it does three things.  A new torch bumps a dependency-generation counter, upgrades torch in the
shared env, and adds the canary branch to tonight's work even when its tip has not moved.  When the
canary branch's tip moved as well, the previous tip is re-measured under the new torch first, which
separates the dependency's effect from the code's.  Every `DEP_FULL_REFRESH_DAYS` days, every
dependency is eager-upgraded and the canary tip is re-measured, which catches drift in packages
other than torch.

A canary run keeps its own run file.  Its name gains a `_gNNNN` suffix, so two runs of one commit
under different dependency sets do not collide, and the dashboard marks it as a dependency change
rather than a code change.  The canary's own bookkeeping lives in `state/<plat>/depgen`,
`torch_seen`, `torch_seen_python`, and `last_full_refresh`.

## Verify before scheduling


```
REG_SMOKE=1 bash tooling/regression/run_regression.sh   # a one-cell plumbing check, no push
action_scripts/run_one_night.sh                          # one real pass: clone, test, measure, push
```

Confirm that a `results/<plat>/<branch>/regression_<plat>_<...>.yaml` appears and that
`state/<plat>/<branch>` updates.  A second immediate run should report no changed branch, which is
fire-on-change working.

## Trial knobs

These exist for pre-schedule runs.  Production leaves all of them unset.

| knob | effect |
|---|---|
| `REG_SMOKE=1` | a toy one-cell sweep into a temp directory, for plumbing checks |
| `REG_NO_PUSH=1` | write results and state locally, and skip the commit and push |
| `REG_FORCE=1` | treat every tracked branch as changed, so an unmoved tip is re-measured |
| `REG_VENV=<dir>` | use an existing venv instead of the dedicated conda env |
| `REG_NO_WATCHDOG=1` | skip the dependency-watch watchdog line |

## Notes and current limits

The macOS schedule runs at `MACOS_NIGHTLY_TIME`.  Pick a time the Mac is normally awake.  A
scheduled wake from sleep is a dark wake, and launchd will not run a LaunchAgent during one, so a
middle-of-the-night time on a laptop that sleeps never fires.  The launchd agent also runs from an
entry clone outside `~/Documents`, because macOS privacy protection stops a launchd process from
reading anything there.  `lib_mac_entry.sh` explains that in full.

The cluster ignores `MACOS_NIGHTLY_TIME` and uses the `POLL_SCHEDULE` cron expression, passed
straight to `scrontab`.  Its job uses QoS `normal`, because the `ai` H100 partition rejects
`standby`.  Most of the cost is the measurement sweeps; fire-on-change exits in seconds otherwise.

Per-branch test results are logged but not gated or diffed.  The engine gate is the alert path.

The engine gate compares each run only against that branch's own previous run.  The dashboard layers
on the broader correctness checks, which are against `main`, single device against multiple devices,
and CPU against GPU.  All of them are derived from the tracked runs themselves, with no
hand-captured reference snapshots.
