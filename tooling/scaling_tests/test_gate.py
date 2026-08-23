"""Tests for the rolling-min memory gate in performance_tracking.py.

Runnable two ways:
  * ``pytest tooling/scaling_tests/test_gate.py``
  * ``python tooling/scaling_tests/test_gate.py``   (prints a per-case trace)

Background: a per-run memory transient would make a single-shot memory compare false-fire.
The gate can roll a MIN over the last ``Config.mem_gate_window`` runs instead, applied to both
sides and to memory only.  A one-run transient then cannot false-fire, while a real floor shift
is still caught with about (W-1) runs of lag.  These tests cover the window at every setting,
including W=1, which is this engine's default because torch's max_memory_allocated showed no
such transient.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import performance_tracking as pt   # noqa: E402
import scaling_common as sc          # noqa: E402

_KEY = dict(geometry="parallel", op="back", size="513x449x385", n_devices=2)


class _Cfg:
    """Minimal stand-in for Config: only the fields the gate reads."""
    mem_hard_pct = 8.0
    mem_gate_window = 4
    speedup_warn_pct = 15.0
    time_soft_pct = 25.0
    fp_rtol_single = 1e-5
    fp_rtol_iter = 1e-4
    compare_to_prior = True


def _cell(mem):
    # an "ok -> ok" cell: only mem_mb varies; matching fingerprint(None)/is_sharded/min_ms/speedup so
    # the memory gate is the sole variable.
    return {**_KEY, "mem_mb": mem, "min_ms": 36.0, "mean_ms": 36.0, "speedup": 1.0, "is_sharded": True}


def _run(mem):
    return {"platform": "gpu", "cells": [_cell(mem)]}


def _gate(tonight_mem, prior_mems_newest_first, W):
    """Simulate run()'s gate path: write priors to a temp dir, window, gate.  prior_mems[0]=p1."""
    d = tempfile.mkdtemp()
    n = len(prior_mems_newest_first)
    for i, mem in enumerate(prior_mems_newest_first):   # newest-first -> tag so lexical asc = oldest..newest
        sc.save_yaml(os.path.join(d, f"regression_gpu_D{n - i:02d}.yaml"), _run(mem))
    priors = pt._find_priors(d, "gpu", "D99", W)         # D99 = tonight (sorts after all priors)
    ref = sc.load_yaml(priors[0]) or {} if priors else {}
    tonight = _run(tonight_mem)
    if not priors:
        return pt.gate_run(tonight, [], _Cfg())["result"], priors
    gr, gref = (tonight, ref) if W <= 1 else pt._apply_mem_window(tonight, ref, priors, W)
    return pt.gate_run(gr, [("prior", gref)], _Cfg())["result"], priors


# floor 883, sporadic transient 986; the floor is HIT within the window (p1,p3,p4 = 883)
_TRANSIENT = [883.4, 986.3, 883.4, 883.4]


def test_find_priors_count_and_order():
    _, priors = _gate(986.3, _TRANSIENT, W=4)
    assert len(priors) == 4
    assert priors[0].endswith("D04.yaml")   # newest first (immediately-prior)


def test_rolling_min_kills_transient():
    res, _ = _gate(986.3, _TRANSIENT, W=4)
    assert res == "pass", f"rolling-min should not gate a one-run transient over a present floor, got {res}"


def test_single_shot_still_gates_transient():
    # W=1 = legacy behaviour: the +11.7% transient MUST fire (proves the fix is what changed).
    res, _ = _gate(986.3, _TRANSIENT, W=1)
    assert res == "fail", f"single-shot must gate the transient, got {res}"


def test_real_floor_shift_is_caught():
    # permanent 883->1000: current window all-1000, reference window still holds one 883 -> fires.
    res, _ = _gate(1000.0, [1000.0, 1000.0, 1000.0, 883.4], W=4)
    assert res == "fail", f"a real floor shift must still be caught, got {res}"


def test_deterministic_cell_never_gates():
    res, _ = _gate(883.4, [883.4, 883.4, 883.4, 883.4], W=4)
    assert res == "pass", f"identical mem must not gate, got {res}"


def test_empty_prior_does_not_crash():
    # a 0-byte immediately-prior YAML (killed mid-write) must degrade gracefully, not crash the run.
    d = tempfile.mkdtemp()
    open(os.path.join(d, "regression_gpu_D01.yaml"), "w").close()
    priors = pt._find_priors(d, "gpu", "D99", 4)
    ref = sc.load_yaml(priors[0]) or {}
    gr, gref = pt._apply_mem_window(_run(986.3), ref, priors, 4)
    res = pt.gate_run(gr, [("prior", gref)], _Cfg())["result"]
    assert res in ("warn", "pass"), f"empty prior should degrade gracefully, got {res}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS  {fn.__name__}")
    print(f"\nALL {len(fns)} GATE TESTS PASSED")
