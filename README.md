# mbirtorch_metrics

The performance and correctness dashboard for
[mbirtorch](https://github.com/cabouman/mbirtorch).  It is an automatically updated record of how
fast, how memory-hungry, and how **correct** mbirtorch's reconstruction operators are over time, on
both CPU and GPU.

**Live dashboard:** <https://cabouman.github.io/mbirtorch_metrics/>

The dashboard rebuilds and republishes automatically whenever new measurements are pushed, so that
link is always current.  You do not need to run anything to read it.

This is a separate repository from mbirtorch itself.  Keeping the performance time series out of the
library's history means the data survives branch churn and is never pushed to the library's `main`.

## How to read it

The dashboard explains itself.  Open the live page and expand the **"How to read this dashboard"**
panel at the top.  It walks through the tiles, the red correctness banner, the History and Scaling
views, and the colours and marks.

That guide lives inside the dashboard, authored in
[`tooling/dashboard/template.html`](tooling/dashboard/template.html) and shipped in the page itself,
so it cannot drift from the UI it describes.  That is why it is not duplicated here or in the
mbirtorch docs; both point to the live page.

## Running or extending it

You do not need a server.  The dashboard is a single self-contained page generated from the YAML
time series in `results/`.

- Build it locally: `action_scripts/build_dashboard.sh`, then open `dashboard/index.html`.
- Add a one-off measurement, run the nightly by hand, or check the schedule: see
  **[`action_scripts/README.md`](action_scripts/README.md)**.
- How runs are measured, gated, and scheduled: see the READMEs under **[`tooling/`](tooling/)**.

## Relationship to mbirjax_metrics

This repository is a port of [mbirjax_metrics](https://github.com/gbuzzard/mbirjax_metrics) at its
commit `e37bc93e`, which is the last commit before that repository grew a second, mbirtorch-specific
nightly.  The two repositories now run independently.  `mbirjax_metrics` records mbirjax and this one
records mbirtorch.  The design record for the port is
`mbirtorch_plans/plans/mbirtorch_metrics/mbirjax_port.md`.

The measured runs from 2026-08-05 onward were migrated from `mbirjax_metrics`, where they were filed
under the platform keys `gpu-torch` and `cpu-torch`.  This repository records one backend, so those
keys are `gpu` and `cpu` here.  The migration renamed the two keys wherever they appeared, which
covers directory names, file names, the `platform` fields, the `sizes` keys in each run's config
block, the `device_label` text, and the recorded `out_dir` paths.  It changed nothing else, and no
measured value moved.
