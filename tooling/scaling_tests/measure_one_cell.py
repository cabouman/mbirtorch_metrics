#!/usr/bin/env python3
"""Ad-hoc: measure ONE nightly cell group, e.g.

    === parallel | forward | 200x208x160 @ n=[1, 2, 4] ===

Reproduces exactly what the nightly measures for a single (geometry, op, size).  It calls the
engine's own ``performance_tracking.measure_cell_group``, so the model build, the inputs, the
device pin, the warmup, and the timing loop are the nightly's, and the numbers are comparable
to the regression YAMLs.  Measuring one cell lets a slowdown be bisected without a full run.

It measures whatever mbirtorch is importable in the CURRENT environment, with no clone and no
PYTHONPATH handling of its own.  To bisect: ``pip install -e`` the commit under test, then run
this.  It runs in this process rather than a worker subprocess, so the memory reading may differ
slightly from the nightly's per-cell isolation.  The TIMING is what this is for.

Usage:
    python measure_one_cell.py                              # parallel | forward | 200x208x160 @ [1,2,4]
    python measure_one_cell.py --op back --size 512x448x384
    python measure_one_cell.py --geometry cone --device-counts 4 2 1 --trials 5
"""
import argparse
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import performance_tracking as pt   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--geometry", default="parallel", help="parallel | cone | denoiser")
    ap.add_argument("--op", default="forward",
                    help="direct_filter | forward | back | vcd_nonconst | denoise")
    ap.add_argument("--size", default="200x208x160", help="e.g. 200x208x160 (denoiser: image shape)")
    ap.add_argument("--device-counts", type=int, nargs="+", default=[1, 2, 4],
                    help="device counts to sweep (the descent is OOM-aware, largest count first)")
    ap.add_argument("--trials", type=int, default=None, help="override trials per op (e.g. 1 or 5)")
    ap.add_argument("--warmup", type=int, default=None, help="override warmup iterations")
    args = ap.parse_args()

    import torch
    platform_key = "gpu" if torch.cuda.is_available() else "cpu"

    config = pt.Config()
    if args.trials is not None:
        config.trials_by_op = {k: args.trials for k in config.trials_by_op}
        config.single_trial_sizes = []        # don't let the 1024 single-trial rule override --trials
    if args.warmup is not None:
        config.warmup = args.warmup

    dc = sorted(set(args.device_counts))
    print(f"\n=== {args.geometry} | {args.op} | {args.size} @ n={dc} ===")
    # Record the runtime stack with every measurement.  This is the variable to bisect when a
    # performance shift survives pinning the code.
    print(f"torch {torch.__version__} · cuda {getattr(torch.version, 'cuda', None)} · platform {platform_key}")
    fd, tmp = tempfile.mkstemp(suffix=".yaml", prefix="perf_adhoc_")
    os.close(fd)
    try:
        res = pt.measure_cell_group(config, args.geometry, args.op, args.size, dc,
                                    platform_key, tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    rows = res.get("rows") or []
    pt.sc.annotate_speedups(rows)             # 'speedup' vs the fewest-device run, as the nightly does
    print(f"\nsummary ({args.geometry} | {args.op} | {args.size}):")
    for r in sorted(rows, key=lambda r: r["n_devices"]):
        mn, mem, sp = r.get("min_ms"), r.get("mem_mb"), r.get("speedup", 1.0)
        print(f"  n={r['n_devices']}   min={mn:9.1f} ms   mem={mem:9.1f} MB   speedup={sp:.2f}x")
    for f in res.get("failures") or []:
        print(f"  n={f['n_devices']}   FAILED{' (OOM)' if f.get('oom') else ''}: {f.get('error')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
