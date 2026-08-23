"""
tooling/scaling_tests/run_performance_local.py
──────────────────────────────────────────────
Manual launcher for the performance_tracking engine, measuring the CURRENT working tree.

Use this to measure in-progress changes without touching the nightly results.  It runs
against whatever mbirtorch is importable in the active environment, and it forces the
output into ``results/manual/<RUN_TAG>/`` with a timestamped filename.  Repeated runs
therefore accumulate side by side instead of overwriting each other or the nightly files.

    python tooling/scaling_tests/run_performance_local.py

Edit the CONFIG block below.  It overrides a subset of performance_tracking.Config, and
every field left unset keeps the engine default.
"""
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import performance_tracking as pt   # noqa: E402
import scaling_common as sc         # noqa: E402


# ── CONFIG (edit here; a subset of performance_tracking.Config) ───────────────
GEOMETRIES = ["parallel", "cone"]
OPS = ["direct_filter", "forward", "back", "vcd_nonconst"]
DEVICE_COUNTS = [1]

# Per-platform SINOGRAM sizes (n_views, n_rows, n_channels), keyed 'cpu' and 'gpu'.
# None keeps the engine's default sizes.  Override for a quick, small local run.
SIZES = None
# Example small override:
# SIZES = {"cpu": [(64, 56, 48)], "gpu": [(256, 224, 192)]}

RUN_TAG = "local"       # output -> results/manual/<RUN_TAG>/
VCD_ITERATIONS = 3


def local_platform_key():
    """'gpu' when torch can use CUDA here, else 'cpu'.

    The nightly has its platform key declared by the wrapper and verified against the
    hardware.  A local run has no wrapper, so it reads the hardware directly.  The output
    goes under results/manual/, which makes no platform claim, so the out-dir guard is not
    involved either way.
    """
    import torch
    return "gpu" if torch.cuda.is_available() else "cpu"


def main():
    platform_key = local_platform_key()
    overrides = dict(
        geometries=GEOMETRIES,
        ops=OPS,
        device_counts=DEVICE_COUNTS,
        run_tag=RUN_TAG,
        vcd_iterations=VCD_ITERATIONS,
        gate=False,             # informational diff only; a local run never fails the process

        # Isolated output: never a nightly results/<plat>/<branch>/ directory.  The timestamped
        # date keeps repeated manual runs from clobbering one another.
        out_dir=os.path.join(sc.RESULTS_DIR, "manual", RUN_TAG or "local"),
        date=datetime.now().strftime("%Y%m%d_%H%M%S"),
        lib_root=os.environ.get("MBIRTORCH_ROOT", ""),
    )
    if SIZES is not None:
        overrides["sizes"] = SIZES
    config = pt.Config(**overrides)
    print("=" * 72)
    print("  performance_tracking — MANUAL local run (current environment)")
    print(f"  out_dir:  {config.out_dir}")
    print(f"  platform: {platform_key}")
    print("=" * 72)
    pt.run(config, platform_key)


if __name__ == "__main__":
    main()
