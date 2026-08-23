# Metrics dashboard generator

`build_dashboard.py` turns the YAML performance time series in this repo into a single, self-contained
`dashboard/index.html` — data, styles, scripts, and the charting library all inlined, so it opens
straight from `file://` with no server or network. (For how to **read** the dashboard, see the repo
[README](../../README.md).)

## Building it

**It's automatic.** Whenever new measurements are pushed to `main`, a GitHub Action runs
`build_dashboard.py` and deploys the result to GitHub Pages (`.github/workflows/pages.yml`) — so the
[published dashboard](https://cabouman.github.io/mbirtorch_metrics/) is always current. You normally never
build it by hand.

### Build it locally

To preview results before they're pushed, from any Python with PyYAML installed:

```bash
action_scripts/build_dashboard.sh        # -> dashboard/index.html
open dashboard/index.html                # macOS; or double-click it
```

`build_dashboard.sh` is a thin wrapper around `build_dashboard.py` (runnable directly); it also prints a
`CORRECTNESS ALERT` summary + the cross-device noise floor. `dashboard/` is gitignored (derived from the
tracked YAML); the source here is what's committed. Tunables (e.g. the correctness tolerances) live in
constants near the top of `build_dashboard.py` — no CLI args.

## What it reads

| source | used for |
|---|---|
| `results/<CPU \| GPU>/<branch>/regression_*.yaml` | per-run configs, gate, and output fingerprints (the time series) |
| `results/<CPU \| GPU>/<branch>/records_<CPU \| GPU>.yaml` | best-ever record book (the "best-ever" reference) |
| `results/<CPU \| GPU>/<branch>/tests_<CPU \| GPU>_<date>.txt` | pytest counts + failing node-ids |
| `results/correctness_acks.yaml` | the `cleared_through` watermark (which correctness divergences are acknowledged) |

Everything the dashboard shows is **derived from the run files** — the "compare against" overlays (main
/ prerelease / prior / best-ever) and the four correctness references (prior run · main · cross-device
n>1-vs-n=1 · cross-platform CPU↔GPU) alike. There are no separate reference snapshots.

## Files

| file | role |
|---|---|
| `build_dashboard.py` | the reader/generator + the corpus correctness analyzer (run this) |
| `clear_correctness.py` | guided "acknowledge divergences through a date" (via `action_scripts/clear_correctness.sh`) |
| `template.html` | page skeleton with `{{...}}` inline placeholders |
| `dashboard.css` | styles (light + dark) |
| `dashboard.js` | client logic for the tiles, History, and Scaling views |
| `vendor/uPlot.iife.min.js`, `vendor/uPlot.min.css` | vendored charting lib ([uPlot](https://github.com/leeoniya/uPlot) v1.6.31, MIT) |
