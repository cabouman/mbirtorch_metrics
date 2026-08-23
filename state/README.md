# `state/` — fire-on-change markers

This directory is the nightly harness's **fire-on-change bookkeeping**.  It records the last commit
each tracked branch was **measured** at, so a run can skip a branch whose tip has not moved.

```
state/<platform>/<branch-slug>     # plain text: the full git sha last MEASURED for that branch+platform
```

- One file per **platform** (`cpu`, `gpu`) and **branch**.  The slug is the branch name with `/`
  replaced by `_`.  Each file contains a single commit sha and nothing else.
- **Read and written only by**
  [`tooling/regression/run_regression.sh`](../tooling/regression/run_regression.sh).  Each run does
  `git ls-remote` for a branch's tip and **skips it if the tip equals the stored sha**.  After a
  successful measurement it writes the measured sha back.  That write is the last step, so a crash
  mid-run re-measures next time.  The markers are committed and pushed alongside `results/`.
- **`action_scripts/add_run.sh` does not touch this directory.**  A hand-seeded backfill writes to
  `results/` and is deliberately not recorded here, so the nightly's change detection is unaffected.

Implications when you are working on the harness:

- To **force a re-measure** of a branch, delete its `state/<plat>/<branch>` file, or set `REG_FORCE=1`
  for one run.
- These are **data, not config**.  Do not hand-edit them to fix the dashboard.  The dashboard reads
  `results/` and never reads `state/`.
- This on-disk `state/` is unrelated to the `ui_state` object in `tooling/dashboard/dashboard.js`,
  which is the client's UI selection.  That is a different layer with a different lifetime.
