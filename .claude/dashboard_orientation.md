# mbirtorch_metrics — orientation for a modifying session

This is the **architecture + extension map + gotchas** for picking up this repo to *change* it.
It deliberately does **not** cover usage / how to read the dashboard / how to run the nightly — that's
in the READMEs and the in-dashboard "How to read this" panel. Read those for *what it does*; read this
for *where things are and how to change them safely*.

READMEs (don't duplicate — go here for the "what"):
- [`README.md`](../README.md) — top-level: how to read the published dashboard (user-facing).
- [`action_scripts/README.md`](../action_scripts/README.md) — the entry-point scripts + the `run_configs.env` knob table.
- [`tooling/dashboard/README.md`](../tooling/dashboard/README.md) — the dashboard generator + its files.
- [`tooling/regression/README.md`](../tooling/regression/README.md) — the nightly harness (schedule, one-time setup).

Two repos: **this one** = harness + dashboard + the result data. **`mbirtorch`** (sibling under
`Research/`) = the library *under test* — never edited here; the nightly clones the tip of each
changed branch into a throwaway directory and measures it.

Published dashboard: <https://cabouman.github.io/mbirtorch_metrics/>

---

## 1. Architecture & data flow

```
 mbirtorch (library under test)      mbirtorch_metrics (this repo)
   branch tips ──┐
                 │  fire-on-change: git ls-remote vs state/<plat>/<branch>
                 ▼
       tooling/regression/run_regression.sh   (the nightly orchestrator)
         · fresh-clone the changed branch into a throwaway worktree
         · lib_env.sh → pip install -e it into the DEDICATED conda env
         · run the engine ─────────►  tooling/scaling_tests/performance_tracking.py
                                        measures geom × op × size × ndev "cells"
                                        each = min time · peak mem · output fingerprint
                                              │
                                              ▼
                          results/<plat>/<branch>/regression_<plat>_<commitUTC>_<sha8>.yaml
                          (+ records_<plat>.yaml best-ever, + state/<plat>/<branch> marker)
                                              │  git push  (push failure is non-fatal, retried)
                                              ▼
              .github/workflows/pages.yml  →  tooling/dashboard/build_dashboard.py
                          reads every YAML, runs the corpus correctness analyzer,
                          inlines data+css+js+uPlot into ONE file
                                              ▼
                          dashboard/index.html  (self-contained, file://-openable)  →  GitHub Pages
```

Three subsystems, three directories — the boundaries matter when changing things:
- **`tooling/scaling_tests/`** — the measurement **engine** (pure Python; spawns per-config worker
  subprocesses so peak memory is per-config). Owns *what is measured and how*.
- **`tooling/regression/`** — the **nightly** wrapper (shell + a status reader). Owns *when/where it
  runs, cloning, installing, pushing*. Knows nothing about plot rendering.
- **`tooling/dashboard/`** — the **generator** (Python build + client JS). Owns *the corpus view +
  correctness analysis + rendering*. Reads only the pushed YAML; writes only `index.html`.

---

## 2. Core concepts & vocabulary

- **Cell coordinate**: `geom|op|size|ndev` (e.g. `cone|vcd_nonconst|512x448x384|4`). The atomic unit
  everywhere — keys, fingerprints, gate hits, dashboard points.
- **Geometries**: `parallel, cone, translation, multiaxis_parallel, denoiser`. Per-geometry overrides
  in `Config`: `geom_ops` (which ops it runs) and `geom_sizes` (its own sizes, since e.g. the denoiser's
  "size" is an *image* shape, not a sinogram). `geom_sizes[g][plat]`'s largest CPU size is mirrored as
  the first GPU size → the shared cell the **cross-platform** check needs.
- **Fingerprint** (`fingerprint()`): float64 `sum/mean/l2norm` + sampled values + `shape/dtype/
  padding_zero`, computed on the op's **true (unpadded) shape** (`op_true_shape`), so it's comparable
  across device counts. The correctness signal.
- **Device pin** (`pin_devices`): every measured row calls `configure_devices` and then asserts the
  realized device list, count and kind. An unpinned mbirtorch model auto-widens on a multi-GPU node,
  so without the pin an all-device run would be filed under a cell labelled n=1. The one unpinned
  settle per night is the `auto-choice` worker, which is what covers the automatic path.
- **Two gates, different jobs**:
  - *Engine gate* (`REG_GATE`, `Config.gate`): compares each run only to **this branch's own prior
    run** (commit-over-commit); sets the process exit code (the nightly's alert). Tolerances
    `fp_rtol_single` / `fp_rtol_iter`.
  - *Dashboard correctness analyzer* (`_analyze_correctness`): four references over the whole corpus —
    **prior**, **vs_main**, **cross_device** (n>1 vs n=1), **cross_platform** (CPU↔GPU same commit).
    Tolerances `XDEV_RTOL`, `VSMAIN_RTOL_{SINGLE,ITER}`, `VSPLAT_RTOL_{SINGLE,ITER}`.
- **Severity split**: correctness ≠ performance. Correctness is the louder signal (red banner + tab
  badge + its own tile); perf regressions are separate.
- **Ack watermark**: `results/correctness_acks.yaml` → `cleared_through: <date>`; every divergence on a
  commit dated ≤ that is treated as reviewed (`_read_cleared_through`, `action_scripts/clear_correctness.sh`).
- **`state/` (on disk) vs `ui_state` (in `dashboard.js`)** — two different things, kept distinctly named:
  - **`state/<plat>/<branch>`** — the **fire-on-change marker**: the last commit sha the nightly
    *measured* for that branch+platform; each run compares the live remote tip against it and skips the
    branch if unchanged. Written by `run_regression.sh` **only** — not `add_run.sh` (so a hand-seeded
    commit doesn't count as "measured"). See [`state/README.md`](../state/README.md).
  - **`ui_state`** (a JS object) — the **client UI selection**: which platform/branch/run is shown, the
    open tile (`openTile`/`runKey`), the History group + device count (`histGroup`/`histN`), the Scaling
    op + compare-against (`ref`), etc. Every `render*` reads it; `renderAll()` redraws the page from it.
    To change a view: mutate `ui_state`, then re-render.

---

## 3. File map (one level deeper than the README file lists)

| file | role | you'd change it to… |
|---|---|---|
| `tooling/scaling_tests/performance_tracking.py` | the engine: `Config`, `make_model`, `pin_devices`, `measure_cell_group`, `fingerprint`, `run_*` timed ops, the gate, the record book, `run()`, `main()` | add a geometry/op/size, change what's measured or fingerprinted, tune engine-gate tolerances |
| `tooling/scaling_tests/scaling_common.py` | process plumbing: `run_worker`, `time_op`, `peak_memory_mb`, `run_measure_loop`, the GPU health sampler, YAML I/O | change worker isolation / timing / memory measurement |
| `tooling/scaling_tests/run_performance_local.py` · `measure_one_cell.py` | manual entry points (edit-the-`CONFIG` local sweep; one cell group) | run a scoped manual measurement |
| `tooling/regression/run_regression.sh` | the nightly orchestrator (fire-on-change → clone → install → measure → push) | change the run flow, gating, or push logic |
| `tooling/regression/lib_env.sh` | shared dedicated-env + worktree install (`reg_activate_env`, `reg_plat_extras`, `reg_install_lib`) — used by the nightly **and** `add_run.sh` | change how the env is built / the library is installed |
| `tooling/regression/regression.env` | infra config (URLs, `WORK_DIR`, `CONDA_ENV`, schedule) — sources `action_scripts/run_configs.env` | change infra (not run knobs) |
| `tooling/regression/recent_runs.py` | status summary; **reuses** `build_dashboard.collect_data()` (one parser) | change the `status_nightly` table |
| `tooling/dashboard/build_dashboard.py` | generator + correctness analyzer: `collect_data`, `_parse_run`, `_analyze_correctness`, `_correctness_summary`, `build`, `REPO_ROOT`, the tolerances | add/tune a correctness reference, change parsing, the alert summary |
| `tooling/dashboard/template.html` | page skeleton + `{{...}}` placeholders + the single-source "How to read this" panel | edit the reading guide, add a page section/container |
| `tooling/dashboard/dashboard.js` | client: constants (`GEOM_ORDER/GEOM_DASH/GEOM_LABEL/OP_ORDER/HIST_GROUPS`), `ui_state`, `render{Tiles,Detail,History,Scaling,Banner}`, `aggregate` | any view/interaction change |
| `tooling/dashboard/dashboard.css` | styles (light-only by design) | styling |
| `tooling/dashboard/clear_correctness.py` | guided ack tool (writes `cleared_through`) | change the ack flow |
| `action_scripts/*` + `run_configs.env` | thin entry wrappers + the run knobs | add an entry point / knob |
| `results/<plat>/<branch>/` · `state/<plat>/<branch>` · `results/correctness_acks.yaml` | the data: run YAMLs + best-ever records · fire-on-change markers · ack watermark | (data, not code — see overwrite/state gotchas below) |

---

## 4. How to extend it — recipes

The **denoiser** is the most recent full worked example of "add a geometry"; grep for `denoiser` /
`denoise` across the four files below to see every touchpoint in one diff.

**Add a geometry** (the denoiser is the worked example):
1. `Config`: append to `geometries`; add `geom_ops[g]` and `geom_sizes[g]` if it differs from the
   defaults (image-shaped sizes, restricted op set, etc.).
2. `make_model`: add an `elif geometry == g:` branch building the model from `size`.
3. `measure_cell_group`: only if its input/op differ from the projection norm — guard the
   sinogram/indices/partition setup and add the op's input builder + dispatch + an `op_true_shape` entry.
4. `cell_device_counts`: say whether the geometry sweeps more than one device count.
5. Dashboard: add to `GEOM_ORDER`, `GEOM_DASH`, `GEOM_LABEL`, `OP_ORDER`, and (if it gets a History
   trend) a `HIST_GROUPS` entry. Scaling's op dropdown + the device selector are data-driven.

**Add an op**: `Config.ops`/`geom_ops` + `trials_by_op`; a `run_<op>` timed helper; a `measure_cell_group`
dispatch branch + `op_true_shape`; `OP_ORDER`; pick the fingerprint tolerance class (single vs iterative)
in **both** the engine gate (`fp_rtol_*`) and the analyzer (the `op in (...)` iterative sets).

**Add / tune a correctness reference**: edit `_analyze_correctness`. **Calibrate tolerances from data,
not guesses** — the build prints the live **cross-device** and **cross-platform** floors; collect every
reldiff into a `*_diffs` list (see `xdev_diffs`/`xplat_diffs`), then set the `*_RTOL` ~10–20× above the
observed floor. Iterative ops (vcd, denoise) sit higher than single-shot.

**Add a dashboard tile/view**: a `render*` function + a `ui_state` field + a container in `template.html` +
css. Everything is **derived** from `collect_data()`'s run records (no separate data source); add it to
the `renderAll()` chain.

---

## 5. Load-bearing gotchas

- **Editable installs beat `PYTHONPATH`.** A modern `pip install -e` registers a `sys.meta_path`
  finder consulted *before* `PYTHONPATH`, so you select the code under test by installing the worktree
  into the **dedicated** env (`lib_env.sh`), never the dev env. (This silently mis-measured `add_run`
  before the fix.) → `mbirtorch_plans/.claude/lessons.md`, §9 "Tooling / harness".
- **Seed `np.random` before any RNG-built partitions.** The denoiser builds VCD partitions from the
  global RNG; unseeded, the sharded-vs-n=1 reldiff is ~1e-4 (noise), not ~1e-7. `run_denoise` seeds;
  `vcd_nonconst` instead passes pre-built partitions.
- **Fingerprints live in the run's private `_fps` index** after `_parse_run` (stripped from the slim
  cells the dashboard renders). Read `run["_fps"][cellkey]`, not `cell["fingerprint"]`.
- **Run-file name = `<commitUTC>_<sha8>`.** Re-measuring the **same commit overwrites** its file; a new
  commit makes a new timeline point. (So `add_run main` on an unchanged `main` refreshes in place.)
- **`add_run.sh` writes `results/` but not `state/`** — the nightly's fire-on-change won't treat a
  hand-seeded commit as "measured," and won't re-measure unless the tip moves.
- **`dashboard/index.html` is gitignored / derived.** Edit `template.html` / `dashboard.{js,css}` /
  `build_dashboard.py`, then rebuild. Never hand-edit `index.html`.
- **Sharding is per-branch.** An unsharded branch (`main`) measures n=1, and `output_sharded` /
  `configure_devices` may be **absent** there — guard with the `try/except TypeError` fallback
  (`run_filter`, `run_denoise` do this).
- **Reproducible fingerprints need deterministic inputs** (`input_seed`, `measure_seed`, fixed
  iterations). Any nondeterminism breaks the prior/vs-main/cross-* comparisons. → memory
  `performance_tracking.md` (the VCD-partition reproducibility note).
- **CPU perf is measured on an interactive laptop — a whole-suite slowdown is the ruler, not the
  code.** Concurrent Mac activity (a manual daytime `run_one_night`, IDE indexing, a browser) injects
  a broad *multiplicative* slowdown into that run's CPU times. The giveaway that it's the ruler and
  not a regression: **~every cell moves by roughly the same factor, on *both* branches at once** — a
  real regression is cell/op-specific and cannot hit two different-code branches identically. (Seen
  2026-07-10: 123/123 cells slower on each of prerelease and kernel_investigation, median ≈1.45×,
  worst on the *short compute-bound* cells — parallel/forward 128³ ≈2.4× — and milder on the long
  ones. That skew is the fingerprint of **core contention**; thermal throttling would be the opposite,
  worst on the sustained-large cells.) Diagnose by ratio-ing the two runs' per-cell `min time` from
  the `_table.yaml` files: a near-uniform median >1 across ~100% of cells ⇒ contention, stop looking
  at the code. The relevant window is the record's `measured_at`, **not** "load right now."
  Mitigation: let the CPU nightly fire from the idle 2 AM scrontab and avoid manual daytime CPU runs
  (or move CPU measurement to an isolated node like the GPU one). `git_dirty: true` on a manual run is
  usually inert — bash-script edits the engine never executes — so don't over-read it.
- The dashboard is **light-mode only** by design (plots read best on white).

---

## 6. How to verify a change (the toolkit we actually use)

- **One cell, fast:** `python tooling/scaling_tests/measure_one_cell.py --geometry cone --op back
  --size 200x208x160` — the nightly's own `measure_cell_group`, for one cell group, in this process.
- **The gate, with no hardware:** `python tooling/scaling_tests/test_gate.py` covers the rolling-min
  memory window at every setting. The gate is pure, so it can also be replayed over the tracked runs.
- **Full pipeline into a throwaway tree:** set `REG_PLATFORM`, `REG_LIB_ROOT`, `REG_OUT_DIR` and run
  `python tooling/scaling_tests/performance_tracking.py`. Add `REG_SMOKE=1` for a toy one-cell sweep
  that finishes in seconds. Point `build_dashboard.REPO_ROOT` at the temp tree to render it.
  **Rebuild the real dashboard afterward** (the temp build overwrites `dashboard/index.html`).
- **UI:** serve `dashboard/` with a static server and drive it with the preview tools (`preview_eval`
  to click rows / read `ui_state`); headless Chrome `--screenshot` is fine for layout, but `--dump-dom`
  hangs on this machine. → memory `dashboard-verify-gotchas.md`.
- Correctness floors print at the end of every `build()` — the cheapest sanity check after a tolerance
  or analyzer change.

---

## 7. History & cross-repo pointers

- This repository is a port of `mbirjax_metrics` at its commit `e37bc93e`. The design record for the
  port is `mbirtorch_plans/plans/mbirtorch_metrics/mbirjax_port.md`. Read it for what was renamed,
  what was dropped, and why.
- The torch nightly's own design record, written while it still lived inside `mbirjax_metrics`, is
  `mbirtorch_plans/plans/torch_port/closed/nightly_plan.md`. It explains the cell set, the device
  pinning, the platform guard, and the environment rules that this repository inherited.
- Sibling **`mbirtorch_plans`** repo: `.claude/lessons.md` (§9 "Tooling / harness" has the
  metrics-harness gotchas) and `plans/torch_port/` (the port's status and findings).
- The no-snapshots design is load-bearing: every reference the dashboard compares against is derived
  from the tracked runs themselves. There are no hand-captured reference files anywhere.
