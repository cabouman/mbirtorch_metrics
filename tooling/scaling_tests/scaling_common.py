"""
tooling/scaling_tests/scaling_common.py
───────────────────────────────────────
Process plumbing for the measurement engine.  Nothing here knows about mbirtorch: this
module owns subprocess isolation, YAML input and output, the device memory and timing
rulers, the GPU health sampler, and the small formatting helpers.  The engine that knows
about models and operators is ``performance_tracking.py``.

Two rules shape this module.

The orchestrator must hold no device memory.  Every task that touches torch runs in its
own fresh subprocess through ``run_worker``, so the peak-memory counter a worker reads
covers that worker's work alone.  The worker writes its result as YAML and may rewrite it
as it goes, so a worker that dies partway still returns what it completed.

The memory ruler is the device counter, not the pool.  ``peak_memory_mb`` reads
``torch.cuda.max_memory_allocated`` over the row's pinned devices, which is allocated
bytes rather than reserved bytes.  On CPU there is no such counter, so the reading is
whole-process resident size, which is coarse.  ``mem_kind`` in every row says which ruler
applied, and the gate uses it to decide whether a memory finding is hard or soft.
"""

import os
import gc
import sys
import time
import threading
import resource
import tempfile
import traceback
import subprocess
import platform as _platform

import numpy as np

from ruamel.yaml import YAML, YAMLError


# ── Paths ───────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(_HERE, "results")


def _ensure_dirs():
    os.makedirs(RESULTS_DIR, exist_ok=True)


# ── Which checkout / environment am I running? ────────────────────────────────
def pyproject_version(root):
    """Project version string from ``<root>/pyproject.toml`` (or None).

    ``root`` is the package root, one directory above the ``mbirtorch/`` package, so this
    matches the checkout under measurement rather than whatever is installed in the env.
    """
    import re
    try:
        with open(os.path.join(root, "pyproject.toml")) as f:
            m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', f.read(), re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def installed_packages():
    """Every installed distribution as a sorted ``{name: version}`` dict.

    The run header records the torch and CUDA versions on their own.  This records the WHOLE
    environment, so any night-to-night drift can be attributed to the specific package that moved,
    for example numpy, triton, or a CUDA runtime wheel.  Uses ``importlib.metadata`` in process,
    with no ``pip`` shell-out; names are lower-cased for stable diffing.  Best-effort, returning
    ``{}`` if unreadable.
    """
    out = {}
    try:
        from importlib import metadata as _md
        for dist in _md.distributions():
            try:
                name = (dist.metadata["Name"] or "").strip()
            except Exception:
                name = ""
            if not name:
                continue
            out.setdefault(name.lower(), dist.version)   # first wins on the rare shadowed-install dup
    except Exception:
        pass
    return dict(sorted(out.items()))


# ── Subprocess orchestration (worker isolation) ───────────────────────────────
def run_worker(script_path, worker_args, extra_env=None):
    """Run an op driver in --worker mode as an isolated subprocess.

    Each torch-touching task (the environment probe, the automatic-device-choice
    check, one cell group's measurement) runs in its own fresh process, so the
    orchestrator never holds device memory while a worker measures its peak.  The worker writes its result as YAML to
    a temp file (passed via --out-file) and may rewrite it incrementally, so a
    worker that dies partway (e.g. GPU OOM at the largest config) still returns
    whatever it completed.  The child inherits the current environment plus
    extra_env; the caller is responsible for putting the checkout under measurement
    on PYTHONPATH so the worker's `import mbirtorch` resolves to it.

    Args:
        script_path (str): absolute path to the op driver (its own __file__).
        worker_args (list[str]): args after the script, e.g.
            ['--worker', '--mode', 'measure', '--size', '256x256x256', ...].
            '--out-file <tmp>' is appended automatically.
        extra_env (dict|None): environment overrides for the child.

    Returns:
        (result, returncode): result is the parsed YAML (or None if the worker
        wrote nothing parseable); returncode is the subprocess exit status.
    """
    # Flush any pending orchestrator output first so the worker's live stdout
    # interleaves in the right order even when stdout is a pipe (PyCharm console).
    sys.stdout.flush()
    fd, out_path = tempfile.mkstemp(suffix=".yaml", prefix="scaling_worker_")
    os.close(fd)
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, script_path, *worker_args, "--out-file", out_path]
    proc = subprocess.run(cmd, env=env)
    result = None
    try:
        with open(out_path) as f:
            result = _yaml.load(f)   # None for an empty/never-written file
    except (FileNotFoundError, YAMLError, ValueError):
        result = None
    finally:
        if os.path.exists(out_path):
            os.remove(out_path)
    return result, proc.returncode


def write_worker_result(out_file, data):
    """Worker side: atomically (re)write a YAML result to out_file.

    Written via a temp file + os.replace so a reader (the orchestrator) never
    sees a half-written file even if the worker is killed mid-write.  Safe to
    call repeatedly to publish partial progress.  Uses the same ruamel YAML
    instance as the rest of the harness (readability + consistency); numpy
    scalars are converted to plain Python first via _to_plain so they serialize
    cleanly.  (_yaml and _to_plain are module-level, defined below and resolved
    at call time.)
    """
    tmp = out_file + ".tmp"
    with open(tmp, "w") as f:
        _yaml.dump(_to_plain(data), f)
    os.replace(tmp, out_file)


# ── OOM classification ────────────────────────────────────────────────────────
OOM_MARKERS = ("RESOURCE_EXHAUSTED", "OUT OF MEMORY", "OOM", "BAD_ALLOC",
               "FAILED TO ALLOCATE", "WORK AREA", "SCRATCH ALLOCATOR",
               "FAILED TO CREATE CUFFT")


def is_oom(text):
    """True if ``text`` names a known out-of-memory marker.

    Prefer passing the full traceback rather than ``str(e)``: an OOM often
    surfaces as an unrelated-looking error (e.g. a numpy "setting an array element
    with a sequence") with the real RESOURCE_EXHAUSTED only visible deeper in the
    stack.
    """
    up = text.upper()
    return any(k in up for k in OOM_MARKERS)


# ── GPU health sampling (clocks, temperatures, throttle reasons) ──────────────
_GPU_FIELDS_FULL = ("index,clocks.sm,clocks.mem,temperature.gpu,temperature.memory,"
                    "clocks_throttle_reasons.hw_thermal_slowdown,"
                    "clocks_throttle_reasons.sw_thermal_slowdown,"
                    "clocks_throttle_reasons.hw_power_brake_slowdown,"
                    "clocks_throttle_reasons.sw_power_cap")
_GPU_FIELDS_MIN = "index,clocks.sm,temperature.gpu"
_THROTTLE_NAMES = ("hw_thermal", "sw_thermal", "hw_power_brake", "sw_power_cap")


def gpu_topology():
    """Best-effort GPU topology snapshot for reproducibility.

    Records which physical GPUs the scheduler handed us (UUIDs, via
    ``nvidia-smi -L``) and how they interconnect (``nvidia-smi topo -m``).
    Cross-allocation performance can hinge on this -- e.g. all GPUs on one NUMA
    socket vs split across two changes host-side launch latency, which hits the
    launch-heavy multi-device paths most -- so we log it next to every result.
    Returns ``{}`` when nvidia-smi is unavailable (e.g. CPU runs).
    """
    out = {}
    for key, cmd in (("devices", ["nvidia-smi", "-L"]),
                     ("topo", ["nvidia-smi", "topo", "-m"])):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                out[key] = r.stdout.strip()
        except Exception:   # nvidia-smi missing / CPU node — best effort
            pass
    return out


def _gi(s):
    """Parse an nvidia-smi integer field; None for '[N/A]' / '[Not Supported]' / blank."""
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def sample_gpu_health():
    """Per-GPU clocks (SM + memory, MHz), temps (core + HBM, C), and active throttle reasons, via
    nvidia-smi.  Returns a list of dicts (one per GPU), or ``[]`` when nvidia-smi is unavailable
    (CPU runs).  Falls back to the SM-clock-only query on drivers that lack the richer fields.

    Why the extras: tomography is HBM-bandwidth-bound, so a hot card can keep full SM clock while its
    MEMORY clock throttles and the kernel slows — the SM clock alone hides it.  ``throttle`` lists any
    active hw/sw thermal or power-cap reason, which names the cause instead of leaving us to guess.
    """
    for fields in (_GPU_FIELDS_FULL, _GPU_FIELDS_MIN):
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=" + fields,
                                "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
        except Exception:           # nvidia-smi missing / CPU node — best effort
            return []
        if r.returncode != 0:       # a field unsupported on this driver -> try the minimal set
            continue
        full = fields is _GPU_FIELDS_FULL
        out = []
        for line in r.stdout.strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 3:
                continue
            g = {"index": _gi(p[0]), "sm_mhz": _gi(p[1])}
            if full and len(p) >= 9:
                g["mem_mhz"] = _gi(p[2]); g["temp_c"] = _gi(p[3]); g["mem_temp_c"] = _gi(p[4])
                g["throttle"] = [nm for nm, v in zip(_THROTTLE_NAMES, p[5:9]) if v.lower() == "active"]
            else:                   # minimal query: index, sm_mhz, temp_c
                g["temp_c"] = _gi(p[2])
            out.append(g)
        if out:
            return out
    return []


def throttled_gpus(gpu_health, temp_hot=85, mem_temp_hot=95):
    """GPUs in ``gpu_health`` that look thermally/power throttled or thermally stressed.

    Flags a GPU if nvidia-smi reports ANY active thermal/power throttle reason, OR its core temp is
    >= ``temp_hot``, OR its HBM temp is >= ``mem_temp_hot``.  Temperature is the reliable signal: a
    single SM-clock snapshot taken after the kernels recover misses the throttle dips, and a hot card
    may throttle its MEMORY clock while the SM clock stays high.  Returns the suspect GPU dicts.
    """
    out = []
    for g in gpu_health:
        t, mt = g.get("temp_c"), g.get("mem_temp_c")
        if (g.get("throttle")
                or (t is not None and t >= temp_hot)
                or (mt is not None and mt >= mem_temp_hot)):
            out.append(g)
    return out


def _fmt_hot_gpu(g):
    """One-line summary of a suspect GPU for the worker log."""
    s = f"GPU{g.get('index')} {g.get('temp_c')}C"
    if g.get("mem_temp_c") is not None:
        s += f" (HBM {g['mem_temp_c']}C)"
    s += f" sm={g.get('sm_mhz')}MHz"
    if g.get("mem_mhz") is not None:
        s += f" mem={g['mem_mhz']}MHz"
    if g.get("throttle"):
        s += f" [{','.join(g['throttle'])}]"
    return s


def _worst_gpu_health(samples):
    """Per-GPU worst case across a list of samples (each sample = a list of GPU dicts): MIN clocks,
    MAX temps, and the union of throttle reasons ever seen.  A single post-run snapshot misses the
    throttling (the clock recovers the instant the kernel ends), so we poll DURING the work and keep
    the worst."""
    if not samples:
        return []
    agg = {}
    for snap in samples:
        for g in snap:
            i = g.get("index")
            d = agg.get(i)
            if d is None:
                d = agg[i] = {"index": i, "sm_mhz": None, "mem_mhz": None,
                              "temp_c": None, "mem_temp_c": None, "_thr": set()}
            for k in ("sm_mhz", "mem_mhz"):     # keep the MINIMUM clock seen
                v = g.get(k)
                if v is not None:
                    d[k] = v if d[k] is None else min(d[k], v)
            for k in ("temp_c", "mem_temp_c"):  # keep the MAXIMUM temp seen
                v = g.get(k)
                if v is not None:
                    d[k] = v if d[k] is None else max(d[k], v)
            d["_thr"].update(g.get("throttle") or [])
    out = []
    for i in sorted(agg, key=lambda x: (x is None, x)):
        d = agg[i]; thr = sorted(d.pop("_thr"))
        if thr:
            d["throttle"] = thr
        out.append(d)
    return out


class _GpuSampler:
    """Background poller: while a timed region runs, sample the GPU health every ``interval`` seconds
    and keep the per-GPU worst (see _worst_gpu_health).  start() before the work, stop() after, read
    worst().  The aggregate is [] on CPU nodes (sample_gpu_health returns [])."""
    def __init__(self, interval=1.0):
        self.interval = interval
        self._stop = threading.Event()
        self._samples = []
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            s = sample_gpu_health()
            if s:
                self._samples.append(s)
            self._stop.wait(self.interval)

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def worst(self):
        return _worst_gpu_health(self._samples)


# ── Device memory, timing, and the device-count descent ───────────────────────
def _sync():
    import torch
    if torch.cuda.is_available():
        torch.cuda.synchronize()


def reset_peak_memory(devices):
    import torch
    if torch.cuda.is_available():
        for d in devices:
            if d.type == "cuda":
                torch.cuda.reset_peak_memory_stats(d)


def peak_memory_mb(devices):
    """Peak memory in MB over the row's PINNED devices, and the ruler's name.

    GPU: max over the pinned devices of torch.cuda.max_memory_allocated — ALLOCATED,
    not reserved.  Reading only the pinned devices (rather
    than every visible one, as mbirtorch.get_memory_stats does) keeps an n=2 row from
    picking up a neighbour's allocation.
    CPU: whole-process RSS, coarse — which is why the memory gate is soft there.
    """
    import torch
    if torch.cuda.is_available():
        peak = 0
        for d in devices:
            if d.type == "cuda":
                peak = max(peak, int(torch.cuda.max_memory_allocated(d)))
        return peak / (1024 ** 2), "gpu_peak_per_device"
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 ** 2) if _platform.system() == "Darwin" else rss / 1024
    return rss_mb, "cpu_rss"


def time_op(run_fn, warmup, trials, devices=None):
    """Time run_fn() over warmup + trials iterations, synchronising each result,
    and read the peak memory PER ITERATION.

    The memory discipline is part of the measurement: drop the PREVIOUS
    iteration's result before allocating the next, so the device peak reflects a single
    call (input + one output) rather than two outputs alive at once.  gc.collect() sits
    outside the timed region so it cannot perturb the timing.

    The peak counters are reset before EVERY iteration and read after it, so each
    reading covers exactly one call.  One reading spanning the whole loop cannot say
    which call carried the peak: a 2026-08-19 comparison found a four-device arm
    recording a 26.6 GiB lead-device watermark where a fresh single reconstruction
    peaks at 6.84 GiB, and the spanning read could not localize it.  Per-iteration readings make
    the column mean "one call's peak" and make an inflated iteration visible by
    itself.  On CPU the reset is a no-op and each reading is whole-process RSS, so
    cpu rows keep their coarse cumulative semantics; mem_kind says which ruler
    applied.

    Returns (stats, result, mem): mem carries the warmup iterations' peaks and the
    trial iterations' peaks in MB, in order, plus the ruler's name.
    """
    result = None
    times = []
    mem = {"peaks_warmup_mb": [], "peaks_trial_mb": [], "mem_kind": "n/a"}
    for i in range(warmup + trials):
        result = None
        gc.collect()
        if devices is not None:
            reset_peak_memory(devices)
        t0 = time.perf_counter()
        result = run_fn()
        _sync()
        dt = time.perf_counter() - t0
        if devices is not None:
            peak_mb, mem["mem_kind"] = peak_memory_mb(devices)
            key = "peaks_trial_mb" if i >= warmup else "peaks_warmup_mb"
            mem[key].append(round(float(peak_mb), 1))
        if i >= warmup:
            times.append(dt)
    arr = np.array(times) * 1e3
    return ({"min_ms": float(arr.min()), "mean_ms": float(arr.mean()),
             "std_ms": float(arr.std())}, result, mem)


def run_measure_loop(size_label, device_counts, out_file, build_and_time, header_extra=""):
    """Device-count descent for one problem size.

    Descend so that per-device allocation ascends within this fresh
    process, stop the descent on an OOM (fewer devices need MORE per-device memory),
    publish incrementally so a hard crash still returns the completed configs, sample
    GPU clocks/temps during each timed region, and free between configs.
    """
    desc = sorted(set(device_counts), reverse=True)
    print(f"\n[measure {size_label}{header_extra}]  device counts (descending): {desc}")
    rows, failures = [], []
    mem_kind = "n/a"

    def _publish():
        write_worker_result(out_file, {"size": size_label, "mem_kind": mem_kind,
                                          "rows": rows, "failures": failures})

    gpu_present = bool(sample_gpu_health())
    for n in desc:
        sampler = _GpuSampler().start() if gpu_present else None
        try:
            timed = build_and_time(n)
        except Exception as e:                    # noqa: BLE001 — never abort the sweep
            if sampler:
                sampler.stop()
            import traceback
            tb = traceback.format_exc()
            oom = is_oom(tb)
            failures.append({"n_devices": n, "oom": oom,
                             "error": str(e).replace("\n", " ")[:300], "traceback": tb})
            print(f"  n_devices={n:2d}  {'OOM' if oom else 'ERROR'}: {str(e)[:120]}")
            if not oom:
                print(tb)
            _publish()
            if oom:
                print(f"  stopping descent at {size_label}: fewer-device configs need "
                      f"more per-device memory and would also OOM")
                break
            continue
        if sampler:
            sampler.stop()
        stats, mem_mb, mem_kind, extra = timed
        gpu_health = (sampler.worst() if sampler else []) or sample_gpu_health()
        hot = throttled_gpus(gpu_health)
        rows.append({"n_devices": n, **stats, "mem_mb": mem_mb,
                     "gpu_health": gpu_health, "throttled": bool(hot), **extra})
        print(f"  n_devices={n:2d}  min={stats['min_ms']:9.1f} ms  "
              f"mean={stats['mean_ms']:9.1f} ms  mem={mem_mb:8.1f} MB ({mem_kind})")
        if hot:
            print("  !! THROTTLING — this timing is UNRELIABLE: "
                  + ", ".join(_fmt_hot_gpu(g) for g in hot))
        _publish()
        gc.collect()
    _publish()
    return rows, failures


# ── Speedup annotation ────────────────────────────────────────────────────────
def annotate_speedups(rows, time_key="min_ms", base_key="n_devices", base_val=1):
    """Add a 'speedup' field to each row, relative to the 1-device run.

    speedup = base_time / row_time, where the baseline is the row whose
    base_key equals base_val (the 1-device run by default).  If no such row is
    present (e.g. a custom device sweep that omits 1 device), fall back to the
    row with the smallest base_key value and print a one-line note, so the
    reported factor is never silently mislabeled as "vs 1 device".

    Args:
        rows (list[dict]): sweep rows, each containing base_key and time_key.
        time_key (str): timing field to ratio (default 'min_ms', the best time).
        base_key (str): field identifying the baseline row (default 'n_devices').
        base_val: baseline value to look for (default 1 = single device).

    Returns:
        The base_key value actually used as the reference (base_val, or the
        smallest present if base_val is absent), or None if rows is empty.
    """
    if not rows:
        return None
    base_row = next((r for r in rows if r.get(base_key) == base_val), None)
    if base_row is None:
        base_row = min(rows, key=lambda r: r[base_key])
        print(f"  (note: no {base_key}={base_val} run; reporting speedup "
              f"relative to {base_key}={base_row[base_key]})")
    base_time = base_row[time_key]
    for r in rows:
        r["speedup"] = base_time / r[time_key]
    return base_row[base_key]


# ── YAML I/O ──────────────────────────────────────────────────────────────────
_yaml = YAML()
_yaml.default_flow_style = False


def save_yaml(path, data):
    _ensure_dirs()
    with open(path, "w") as f:
        _yaml.dump(_to_plain(data), f)
    print(f"  wrote {path}")


def load_yaml(path):
    with open(path, "r") as f:
        return _yaml.load(f)


def _to_plain(obj):
    """Recursively convert numpy scalars/arrays to plain Python for YAML."""
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


# ── Problem-size label ────────────────────────────────────────────────────────
# Problem-size *sets* now live at the top of each op driver (different ops want
# different sizes); scaling_common only provides the shared label formatter.
def size_label(size):
    v, r, c = size
    return f"{v}x{r}x{c}"
