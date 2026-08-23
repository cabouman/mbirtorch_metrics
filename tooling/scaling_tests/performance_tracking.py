"""
tooling/scaling_tests/performance_tracking.py
─────────────────────────────────────────────
The nightly and manual REGRESSION engine for mbirtorch.  It sweeps geometry, operator,
problem size, and device count; measures minimum time, peak memory, and a tolerant
correctness fingerprint for every cell; compares the run against this branch's previous
run; and writes one dated YAML plus its companions.

The companions are three.  ``regression_<plat>_<commit-tag>.yaml`` is the run itself.
``<...>_table.yaml`` is the browsable per-run dump.  ``records_<plat>.yaml`` is the
best-ever record book.  The dashboard reads all three.

Two layers live here, and the split is worth knowing when changing the file.

The MEASUREMENT layer builds mbirtorch models, pins their devices, runs the timed
operators, and reads the rulers.  It is the only part that imports mbirtorch, and it does
so inside worker subprocesses.

The DECISION layer is backend-independent.  It holds ``Config``, the correctness
fingerprint, the gate and its thresholds, the record book, the prior-run selection, the
rolling-minimum memory window, and the commit-time file tag.  Nothing in it imports torch.

Roles:
  - orchestrator (default, no args)   : ``run()`` — per (geometry, op, size) spawn a
                                        worker, collect rows, gate, write the YAML.
  - worker --mode setup               : report the environment and the device count.
  - worker --mode auto-choice         : one unpinned settle, judged against the floors.
  - worker --mode measure ...         : measure one cell group (all device counts).

THE CELLS ARE PINNED, ALWAYS.  Every measured row calls ``configure_devices`` and then
asserts the realized device list, because an unpinned mbirtorch model auto-widens on a
multi-GPU node and would file an all-device run under a cell labelled n=1.  The one
unpinned settle per night is the ``auto-choice`` check, which is what covers the automatic
path a user hits by default.

Env vars (set by tooling/regression/run_regression.sh):
  REG_LIB_ROOT   (required)  the mbirtorch checkout under test (PYTHONPATH + provenance)
  REG_OUT_DIR    (required)  results/<plat>/<branch_slug>/
  REG_PLATFORM   (required)  'gpu' | 'cpu' — DECLARED by the wrapper, then verified here
  REG_DATE       (optional)  YYYYMMDD, resolved once by the wrapper
  REG_GATE       (optional)  '1' (default) to exit non-zero on a hard regression
  REG_RUN_TAG    (optional)  branch name recorded in the YAML
  REG_DEVICE_COUNTS (optional)  space-separated, default '1'; n>1 applies only at the
                                MULTI_DEVICE_SIZE_LABELS cells — every other cell stays n=1
  REG_MEM_GATE_WINDOW (optional) rolling-min window in runs; default 1 (see build_config)
  REG_SKIP_TESTS (optional)  '1' when the wrapper owns the test step
  REG_SMOKE      (optional)  '1' -> a toy 1-cell sweep, for plumbing checks
"""
import argparse
import dataclasses
import datetime
import gc
import os
import platform as _platform
import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import scaling_common as sc                # noqa: E402  torch-free at module level


# ── Run configuration ─────────────────────────────────────────────────────────
# The Config defaults encode the NIGHTLY sweep; the manual launcher and main() override a
# subset.  A worker reconstructs this from the temp YAML the orchestrator writes; from_dict
# tolerates extra and missing keys, so the schema can evolve without breaking a serialized
# config or a stored run file.
@dataclass
class Config:
    # sweep dimensions
    geometries: list = field(default_factory=lambda: ["parallel", "cone", "denoiser"])
    ops: list = field(default_factory=lambda: ["direct_filter", "forward", "back", "vcd_nonconst"])
    # Per-geometry op OVERRIDES (else `ops` is used).  The denoiser's only real op is
    # `denoise`, the qGGMRF outer loop with identity projectors, which is the vcd_nonconst
    # analog; its forward and back are the identity and it has no filtered back projection.
    geom_ops: dict = field(default_factory=lambda: {"denoiser": ["denoise"]})
    device_counts: list = field(default_factory=lambda: [1, 2, 4])
    # SINOGRAM sizes (n_views, n_rows, n_channels) — ASYMMETRIC (all three differ) to surface
    # axis swaps; one DIVIDING and one NON-DIVIDING (all-odd) per platform to exercise padding;
    # plus a GPU 1024-class capacity size.  The recon shape is auto-derived per geometry, then
    # pinned for cone (see CONE_RECON_SHAPE_PINS).  The largest CPU size is ALSO measured on GPU
    # (the first GPU entry) so the dashboard's CPU-to-GPU cross-platform correctness check has a
    # shared cell to compare.
    sizes: dict = field(default_factory=lambda: {
        "cpu": [(128, 112, 96), (129, 113, 97), (200, 208, 160)],
        "gpu": [(200, 208, 160), (512, 448, 384), (513, 449, 385), (1024, 1008, 992)],
    })
    # Per-geometry size OVERRIDES (else `sizes[plat]` is used).  The denoiser's size tuple IS the
    # IMAGE shape (rows, cols, slices), not a sinogram, so it needs its own table.
    geom_sizes: dict = field(default_factory=lambda: {
        "denoiser": {
            "cpu": [(128, 144, 160), (225, 241, 257)],
            "gpu": [(225, 241, 257), (512, 448, 384), (1024, 1008, 992)],
        },
    })
    # Sizes where every op runs trials=1 (capacity/memory check, not a timing ruler).
    single_trial_sizes: list = field(default_factory=lambda: ["1024x1008x992"])

    # vcd
    vcd_iterations: int = 3
    weight_mode: str = "nonconstant"
    weight_seed: int = 13

    # denoiser (QGGMRFDenoiser.denoise — the vcd analog with identity projectors).  A FIXED sigma,
    # an EXACT iteration count (stop_threshold_change_pct=0), and a seeded input image make the
    # fingerprint deterministic, so the cross-device and cross-platform checks are meaningful.
    denoise_iterations: int = 20
    denoise_sigma: float = 0.1
    denoise_sharpness: float = 0.0

    # measurement
    warmup: int = 1
    trials_by_op: dict = field(default_factory=lambda: {
        "direct_filter": 3, "forward": 3, "back": 3, "vcd_nonconst": 1, "denoise": 1})

    # geometry / seeds
    cone_sdd_over_channels: float = 4.0
    input_seed: int = 0
    measure_seed: int = 7

    # io / provenance
    out_dir: str = ""      # stable nightly dir, or results/manual/<tag> (required at run time)
    date: str = ""         # stamped by the orchestrator (never datetime.now() in a worker)
    run_tag: str = ""
    # Dependency-canary provenance.  dep_gen is the identity of the installed dependency set,
    # monotonic per platform; when it is above 0 the run filename gets a `_gNNNN` suffix, so
    # several runs of ONE commit with different dependencies do not collide.  Generation 0 is
    # the historical name with no suffix.  run_reason records what triggered the run.
    dep_gen: int = 0
    run_reason: str = "commit"   # "commit" | "torch-step" | "code-step" | "deps-step"
    torch_available: str = ""    # PyPI-latest torch at run time, when a canary reports it.  Records
                                 # what was AVAILABLE even where the install resolved to an older
                                 # torch, so the dashboard can say "X available, installed Y".
    lib_root: str = ""     # the mbirtorch checkout to MEASURE (PYTHONPATH + provenance); required

    # diff / gate
    gate: bool = True               # set the process exit code on a HARD regression
    compare_to_prior: bool = True   # compare against the most-recent prior dated file in out_dir
                                    # (this branch's own previous commit) — the sole gate reference.
                                    # Cross-branch comparison (vs main/prerelease) and best-ever
                                    # drift are surfaced on the dashboard, not gated here.
    mem_hard_pct: float = 8.0       # memory growth threshold (%); HARD on GPU, soft on CPU
    mem_gate_window: int = 1        # rolling-MIN window (runs) for the memory gate; see build_config
    speedup_warn_pct: float = 15.0  # speedup-ratio drop WARN threshold (%); soft on all platforms
    time_soft_pct: float = 25.0     # absolute-time WARN threshold (%)
    fp_rtol_single: float = 1e-5    # fingerprint robust-aggregate rel tol (single-shot ops)
    fp_rtol_iter: float = 1e-4      # ... for the iterated vcd and denoise
    k_sample_tol: int = 1           # allowed deviating fingerprint samples before a soft flag

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in (d or {}).items() if k in names})


# ── Cone recon_shape pins — padding-policy decoupling ─────────────────────────
# The cone auto recon_shape depends on mbirtorch's axial-padding policy, so a change to
# that policy would silently move recon_shape, and with it every cone cell's memory and
# time baseline.  Pin the shape per sinogram size to the shape that ships today, so a
# padding change no longer moves the vs-prior series.  The pinned values equal the current
# auto-derived shapes, so pinning moves no baseline.
CONE_RECON_SHAPE_PINS = {
    (128, 112, 96):    (96, 96, 112),
    (129, 113, 97):    (97, 97, 113),
    (200, 208, 160):   (160, 160, 208),
    (512, 448, 384):   (384, 384, 448),
    (513, 449, 385):   (385, 385, 449),
    (1024, 1008, 992): (992, 992, 1008),
}

# Sizes that sweep MULTIPLE device counts (when REG_DEVICE_COUNTS asks for them).
# The multi-GPU rows exist at the two sizes the torch campaign gates on, where multi-device
# history is directly comparable to the campaign record.  The smaller sizes measure mostly
# communication overhead at n>1 and stay single-device.  The denoiser stays single-device at
# every size: QGGMRFDenoiser.denoise raises under any non-trivial placement, and the
# device-policy work deliberately leaves it outside the widening.
MULTI_DEVICE_SIZE_LABELS = {"512x448x384", "1024x1008x992"}

def cell_device_counts(geometry, size_label, device_counts):
    """The device counts one (geometry, op, size) cell group sweeps."""
    if geometry == "denoiser":
        return [1]
    if size_label in MULTI_DEVICE_SIZE_LABELS:
        return list(device_counts)
    return [1]


def build_config(platform_key, out_dir, date, run_tag, lib_root, device_counts, gate):
    """The nightly sweep as a Config, with the environment's overrides applied.

    Building a real Config, rather than a hand-rolled dict, is what makes the run file carry
    the five gate-threshold keys the dashboard reads for its threshold explanation, and what
    lets _expected_cells reconstruct the sweep this run was supposed to attempt.
    """
    cfg = Config(out_dir=out_dir, date=date, run_tag=run_tag, lib_root=lib_root,
                 device_counts=list(device_counts), gate=gate)
    # Rolling-min memory window.  The window exists to reject a per-run memory transient, and
    # torch's max_memory_allocated does not show one: an ablation of five fresh subprocesses on
    # one cell (H100, parallel/vcd_nonconst at 512x448x384, n=1, 2026-08-08) read a spread of
    # 0.000%, with min equal to max.  A window of 1 is therefore a single-shot compare with no
    # detection lag, which is the default here.
    cfg.mem_gate_window = int(os.environ.get("REG_MEM_GATE_WINDOW") or 1)
    return cfg


# ── Guards ────────────────────────────────────────────────────────────────────
def assert_platform(declared):
    """Abort unless the hardware matches the platform key the WRAPPER declared.

    The platform key is declared, never inferred.  Inferring it from
    torch.cuda.is_available() cannot fail loudly: a GPU night on which CUDA did not
    initialise would quietly file itself under cpu, and the gpu charts would go silent
    with no other symptom.  That failure happened once, on 2026-07-21, in the jax
    nightly this engine is descended from.  ``_assert_platform_matches_out_dir`` is the
    second half of the same guard: this function checks the hardware against the
    declaration, that one checks the declaration against the output directory.
    """
    import torch
    if declared not in ("gpu", "cpu"):
        raise SystemExit(f"REG_PLATFORM must be gpu or cpu, got {declared!r}")
    on_gpu = bool(torch.cuda.is_available())
    want_gpu = (declared == "gpu")
    if on_gpu != want_gpu:
        raise RuntimeError(
            "PLATFORM MISMATCH: the wrapper declared REG_PLATFORM={d!r} but "
            "torch.cuda.is_available() is {a}.\n"
            "  Measuring on one platform and filing under the other would write "
            "records_{d}.yaml into a tree the dashboard reads for the OTHER platform, "
            "and the charts would simply go quiet.  Aborting instead.\n"
            "  If this is a GPU night: check that the node actually allocated a GPU "
            "(no --gpus-per-node, or a CUDA/driver mismatch in the torch build).".format(
                d=declared, a=on_gpu))
    if want_gpu and torch.cuda.device_count() < 1:
        raise RuntimeError("PLATFORM MISMATCH: cuda is available but device_count() is 0.")


def assert_no_calibration():
    """Refuse to measure with mbirtorch's memory-calibration mode on.

    That mode calls reset_peak_memory_stats at the top of _vcd_recon and OWNS the peak
    counter, so it would clobber the very number these rows record.  Checked rather
    than trusted, because it is an ambient environment variable.
    """
    val = os.environ.get("MBIRTORCH_MEMORY_CALIBRATION")
    if val and val.lower() not in ("", "0", "false"):
        raise RuntimeError(
            f"MBIRTORCH_MEMORY_CALIBRATION={val!r} is set.  That mode resets and owns "
            "torch.cuda.max_memory_allocated, which is the memory ruler these rows read. "
            "Unset it before measuring.")


# ── Devices: pin explicitly, then verify what was BOUND ───────────────────────
def pin_devices(model, n, platform_key):
    """Pin the model to EXACTLY n devices of the platform's kind and return the
    realized device list.

    The pin is mandatory on every row, at every count including n=1.  mbirtorch's
    device policy (landed 2026-08-08) gives an unpinned model an all-device default
    on multi-GPU CUDA, with the single device resolved lazily as cuda > mps > cpu.
    Without this call every row would silently measure an all-device run under a
    cell labelled n=1 — the same defect the device-policy design records in
    the device-policy work's own gate readout, where the n=1 arm was also the reference
    the value diffs were taken against.

    The pin must also name the device KIND, not just the count.  On a Mac,
    configure_devices(num_devices=1) binds the lazily-preferred device, which is
    MPS — so a cpu row pinned by count alone would silently measure Apple's
    GPU and file it under cpu.  cpu therefore pins devices=['cpu'] explicitly
    (repeated virtual cpu devices at n>1, as mbirtorch's own sharding tests do).

    configure_devices() sets device_layout_is_automatic=False permanently, which is
    the flag the automatic path consults, so it is the durable pin.  It also rebuilds
    the placements and recreates the projectors, so it must run BEFORE any warmup.

    Verification is the layer that makes the pin checkable: read back what the model
    actually bound, both count and kind, and refuse to measure on any disagreement.
    """
    want_kind = "cuda" if platform_key == "gpu" else "cpu"
    if want_kind == "cuda":
        # n=1 binds cuda:0: assert_platform already proved CUDA is up on this key.
        model.configure_devices(num_devices=n)
    else:
        model.configure_devices(devices=["cpu"] * n)
    devices = list(model.sino_placement.devices)
    if len(devices) != n:
        raise RuntimeError(
            f"DEVICE PIN FAILED: asked for {n} device(s), model bound {len(devices)} "
            f"({[str(d) for d in devices]}).  Refusing to file this row under n={n}.")
    wrong = [str(d) for d in devices if d.type != want_kind]
    if wrong:
        raise RuntimeError(
            f"DEVICE PIN FAILED: platform {platform_key} wants only {want_kind} "
            f"devices, model bound {wrong}.  Refusing to file this row under "
            f"{platform_key}.")
    return devices


def placement_info(model, devices):
    """The per-row record of WHAT WAS BOUND (not what was requested)."""
    return {"is_sharded": not bool(model.sino_placement.is_trivial),
            "n_shard_devices": int(model.sino_placement.n_devices),
            "devices": [str(d) for d in devices]}


# ── Model + inputs ────────────────────────────────────────────────────────────
def make_model(config, geometry, size, platform_key):
    """Build an mbirtorch model of ``geometry`` for SINOGRAM ``size``.

    Mirrors performance_tracking.make_model: the same cone geometry convention
    (magnification 2 via source_detector_dist = 4 * channels), the same recon-shape
    pinning for cone, verbose off.  The constructors take no device argument
    (configure_devices is the single door since the 2026-08-08 policy landing), so
    every model built here MUST be followed by pin_devices before any use — an
    unpinned model resolves its device lazily and, on multi-GPU CUDA, auto-widens.
    """
    import mbirtorch
    n_views, n_rows, n_channels = size
    angles = np.linspace(0, np.pi, n_views, endpoint=False)
    if geometry == "parallel":
        model = mbirtorch.ParallelBeamModel((n_views, n_rows, n_channels), angles)
    elif geometry == "cone":
        sdd = config.cone_sdd_over_channels * n_channels
        model = mbirtorch.ConeBeamModel((n_views, n_rows, n_channels), angles,
                                        source_detector_dist=sdd, source_iso_dist=sdd / 2.0)
        pin = CONE_RECON_SHAPE_PINS.get((int(n_views), int(n_rows), int(n_channels)))
        if pin is not None:
            model.set_params(recon_shape=tuple(int(x) for x in pin), no_warning=True)
        else:
            print(f"WARNING: cone size {(n_views, n_rows, n_channels)} has no recon_shape pin; "
                  f"using auto {tuple(int(x) for x in model.get_params('recon_shape'))} — add it "
                  f"to CONE_RECON_SHAPE_PINS to decouple from padding policy.", file=sys.stderr)
    elif geometry == "denoiser":
        model = mbirtorch.QGGMRFDenoiser(tuple(int(x) for x in size))
        model.set_params(sharpness=config.denoise_sharpness, no_warning=True)
    else:
        raise ValueError(f"unknown geometry {geometry!r} (expected parallel/cone/denoiser)")
    model.set_params(verbose=0, no_warning=True)
    return model


def make_indices(model):
    """Full field-of-view pixel indices over the whole reconstruction region."""
    import mbirtorch
    recon_shape = model.get_params('recon_shape')
    return mbirtorch.gen_full_indices(tuple(int(x) for x in recon_shape),
                                      use_ror_mask=model.get_params('use_ror_mask'))


def make_cylinders(num_pixels, num_slices, seed):
    """Deterministic random recon cylinders (num_pixels, num_slices) float32."""
    rng = np.random.default_rng(seed)
    return rng.standard_normal((num_pixels, num_slices), dtype=np.float32)


def make_sinogram(config, size):
    """Deterministic random sinogram of SINOGRAM ``size`` (numpy float32).

    Projection is linear, so a random sinogram is a valid timing/memory input.
    """
    rng = np.random.default_rng(config.input_seed)
    return rng.random(size, dtype=np.float32)


def make_noisy_image(config, size):
    """Deterministic noisy 3D image (numpy float32) of IMAGE shape ``size`` — the denoiser's input.

    A seeded random image is a valid timing/memory/fingerprint input: with a FIXED ``sigma_noise`` and an
    EXACT iteration count (``stop_threshold_change_pct=0``) the denoise is deterministic, so the output
    fingerprint is reproducible across runs, device counts, and platforms.
    """
    rng = np.random.default_rng(config.input_seed)
    return rng.standard_normal(size, dtype=np.float32)


def make_weights(config, size):
    """Deterministic NONCONSTANT weights (positive) for the weighted VCD path.

    All-ones weights skip the weighted gradient/Hessian path; a seeded uniform draw in
    [0.5, 1.5] exercises it while staying positive and reproducible.
    """
    rng = np.random.default_rng(config.weight_seed)
    return rng.uniform(0.5, 1.5, size=size).astype(np.float32)


def to_device(model, arr, kind):
    """Pre-place a HOST input in the model's device form, OUTSIDE the timing loop.

    Measure the op, not the host->device transfer.  ``kind`` is 'sino' (view axis)
    or 'recon' (slice axis); at n=1 both are a plain tensor on the model's device.
    """
    import torch
    if kind == "sino":
        placed = model._shard_sinogram(arr)
    else:
        placed = model._shard_recon(arr)
    sc._sync()
    return placed


def to_numpy(out):
    """Device form (tensor or Shards) -> numpy, for the fingerprint.

    Test for a tensor FIRST: torch.Tensor also has a .gather method (the indexing
    one), so duck-typing on 'gather' alone calls that with no arguments.

    Shards.gather() already returns NUMPY (it detaches and concatenates on the host
    internally), so it must not be detached again — doing so raised
    "'numpy.ndarray' object has no attribute 'detach'" on every n>1 row of the first
    multi-device trial.  The n=1 path never reaches this branch, which is why the
    single-device verification could not have caught it.
    """
    import torch
    if torch.is_tensor(out):
        return out.detach().cpu().numpy()
    if hasattr(out, "tensors") and hasattr(out, "placement"):   # _sharding.Shards
        return np.asarray(out.gather())
    return np.asarray(out)


# ── Op bodies — the timed calls ───────────────────────────────────────────────
def run_filter(model, sino):
    return model.direct_filter(sino, output_sharded=True)


def run_forward(model, cylinders, pixel_indices):
    return model.sparse_forward_project(cylinders, pixel_indices)


def run_back(model, sino, pixel_indices):
    return model.sparse_back_project(sino, pixel_indices)


def build_partitions(model, sino_np, weights, max_iterations, seed):
    """Build the VCD partitions + sequence once, OUTSIDE the timing loop.

    gen_pixel_partition draws from the un-seeded global RNG, so without the seed the
    partitions — and therefore the recon — vary run to run and the day-over-day VCD
    fingerprint would false-positive.
    """
    np.random.seed(seed)
    ret = model.initialize_recon(sino_np, weights=weights, max_iterations=max_iterations)
    return ret[3], ret[4]        # partitions, partition_sequence


def run_vcd(model, sino_np, weights, partitions, partition_sequence, measure_seed):
    """Timed op: one full VCD reconstruction with NONCONSTANT weights."""
    np.random.seed(measure_seed)
    recon, _stats = model._vcd_recon(sino_np, partitions, partition_sequence,
                                     stop_threshold_change_pct=0.0,
                                     weights=weights, init_recon=None)
    return recon


def run_denoise(model, image, config):
    np.random.seed(config.measure_seed)
    out, _ = model.denoise(image, sigma_noise=config.denoise_sigma,
                           max_iterations=config.denoise_iterations,
                           stop_threshold_change_pct=0.0, output_sharded=True)
    return out


# ── Correctness fingerprint ───────────────────────────────────────────────────
def _crop_to_true_shape(arr, true_shape):
    """Crop a possibly-padded device-form output to the TRUE shape and check the padding.

    At a non-dividing count an op may return the padded device form (e.g. 49->50 views,
    41->42 slices).  The fingerprint must be on the TRUE shape so it is comparable across
    device counts and runs.  Returns ``(cropped, padding_zero)`` where padding_zero is:
      - None  if arr is not padded (shape already == true_shape),
      - True/False whether the padded OVERHANG is exactly 0 (a constructed-zero invariant; a
        non-zero overhang is a real padding-leak bug, surfaced rather than hidden).
    """
    arr = np.asarray(arr)
    true_shape = tuple(int(s) for s in true_shape)
    if arr.shape == true_shape:
        return arr, None
    padding_zero = True
    for ax, (a, t) in enumerate(zip(arr.shape, true_shape)):
        if a > t:   # overhang along this axis must be exactly zero
            overhang = arr.take(range(t, a), axis=ax)
            if not bool(np.all(overhang == 0.0)):
                padding_zero = False
    cropped = arr[tuple(slice(0, t) for t in true_shape)]
    return cropped, padding_zero


def fingerprint(result, true_shape, k_samples=12):
    """Tolerant correctness fingerprint of an op output, computed on the TRUE shape.

    Reductions {sum, mean, l2norm} are accumulated in float64 so the fingerprint reflects the
    array's value, not float32 accumulation order (which varies with device count).  ``samples``
    are the exact values at K evenly-spaced, deterministic flat indices.  ``shape``/``dtype`` are
    the exact (structural) part of the gate.  See _crop_to_true_shape for the padding handling.
    """
    cropped, padding_zero = _crop_to_true_shape(result, true_shape)
    flat = np.asarray(cropped).ravel()
    n = int(flat.size)
    flat64 = flat.astype(np.float64)
    idx = (np.linspace(0, n - 1, min(k_samples, n)).astype(int) if n else np.array([], int))
    return {
        "sum": float(flat64.sum()),
        "mean": float(flat64.mean()) if n else 0.0,
        "l2norm": float(np.sqrt(np.sum(flat64 * flat64))),
        "min": float(flat.min()) if n else 0.0,
        "max": float(flat.max()) if n else 0.0,
        "samples": [float(flat[i]) for i in idx],
        "shape": list(np.asarray(cropped).shape),
        "dtype": str(np.asarray(result).dtype),
        "padding_zero": padding_zero,
    }


def parse_size_label(label):
    """'128x112x96' -> (128, 112, 96)."""
    return tuple(int(x) for x in label.split("x"))


# ── Worker body ───────────────────────────────────────────────────────────────
def measure_cell_group(config, geometry, op, size_label, device_counts, platform_key, out_file):
    """Measure one (geometry, op, size) across ``device_counts``."""
    import mbirtorch  # noqa: F401
    assert_no_calibration()
    size = parse_size_label(size_label)
    is_denoiser = (geometry == "denoiser")

    # Inputs come from the generators above at the config's seeds.  They are pure numpy, so
    # the same arrays reach every device count and every platform.
    sino_np = None if is_denoiser else make_sinogram(config, size)
    image_np = make_noisy_image(config, size) if is_denoiser else None

    base_model = make_model(config, geometry, size, platform_key)
    pin_devices(base_model, 1, platform_key)
    recon_shape = tuple(int(x) for x in base_model.get_params('recon_shape'))
    if is_denoiser:
        idx = cylinders = num_pixels = None
    else:
        idx = make_indices(base_model)
        num_pixels = len(idx)
        cylinders = (make_cylinders(num_pixels, recon_shape[2], config.input_seed)
                     if op == "forward" else None)
    weights = make_weights(config, size) if op == "vcd_nonconst" else None
    del base_model
    gc.collect()

    # TRUE (unpadded) output shape per op, for the fingerprint crop.
    op_true_shape = {
        "direct_filter": tuple(size),
        "forward": tuple(size),
        "back": (num_pixels, recon_shape[2]),
        "vcd_nonconst": tuple(recon_shape),
        "denoise": tuple(recon_shape),
    }.get(op, tuple(size))

    trials = 1 if size_label in config.single_trial_sizes else config.trials_by_op.get(op, 3)

    def build_and_time(n):
        model = make_model(config, geometry, size, platform_key)
        devices = pin_devices(model, n, platform_key)   # pin FIRST, before any warmup
        info = placement_info(model, devices)
        if op == "direct_filter":
            sino_dev = to_device(model, sino_np, "sino")
            run_fn = lambda: run_filter(model, sino_dev)
        elif op == "forward":
            cyl_dev = to_device(model, cylinders, "recon")
            run_fn = lambda: run_forward(model, cyl_dev, idx)
        elif op == "back":
            sino_dev = to_device(model, sino_np, "sino")
            run_fn = lambda: run_back(model, sino_dev, idx)
        elif op == "vcd_nonconst":
            partitions, partition_sequence = build_partitions(
                model, sino_np, weights, config.vcd_iterations, config.measure_seed)
            run_fn = lambda: run_vcd(model, sino_np, weights, partitions,
                                     partition_sequence, config.measure_seed)
        elif op == "denoise":
            run_fn = lambda: run_denoise(model, image_np, config)
        else:
            raise ValueError(f"op {op!r} not implemented")
        # The memory ruler lives INSIDE the timing loop: the counters reset
        # before every iteration and are read after it, so the row's number is
        # the largest single-call peak among the warm trials, and the warmup's
        # own peaks (which include the compiles) are recorded beside it rather
        # than folded in.
        stats, result, mem = sc.time_op(run_fn, config.warmup, trials, devices=devices)
        mem_kind = mem["mem_kind"]
        trial_peaks = mem["peaks_trial_mb"]
        mem_mb = max(trial_peaks) if trial_peaks else 0.0
        # Re-verify the binding AFTER the timed call: a widening that happened inside
        # recon() would not be visible at pin time.
        if int(model.sino_placement.n_devices) != n:
            raise RuntimeError(
                f"DEVICE COUNT CHANGED DURING THE OP: pinned {n}, now "
                f"{int(model.sino_placement.n_devices)}.  Refusing to file under n={n}.")
        # Fingerprint AFTER the memory read, so the host gather cannot inflate the peak.
        fp = fingerprint(to_numpy(result), op_true_shape)
        return stats, mem_mb, mem_kind, {**info, "fingerprint": fp,
                                         "platform": platform_key,
                                         "mem_peaks_warmup_mb":
                                             mem["peaks_warmup_mb"],
                                         "mem_peaks_trial_mb": trial_peaks}

    rows, failures = sc.run_measure_loop(
        size_label, device_counts, out_file, build_and_time,
        header_extra=f" | {geometry} | op={op} | recon={recon_shape}")
    for r in rows:
        r["geometry"] = geometry
        r["op"] = op
        r["size"] = size_label
        r["recon_shape"] = list(recon_shape)
        r["trials"] = trials
    return {"geometry": geometry, "op": op, "size": size_label,
            "recon_shape": list(recon_shape), "rows": rows, "failures": failures}


# The automatic-device-choice check, one per multi-GPU night.  Every measured
# row pins its device count, and a pin bypasses the automatic choice entirely,
# so the path a multi-GPU user hits by default -- the library choosing how many
# devices to use -- would otherwise never run on real hardware on a schedule.
# The check builds one UNPINNED model, lets the settle choose, and compares the
# realized count against what the shipped widening floors say it should be.
# The cell is the 512-class cone reconstruction, because its expected choice
# there is a MIDDLE count (the floors admit two devices and hold four on a
# four-GPU node), so the floors, their ordering, and the capacity search all
# participate in one check.  The expected count is computed at run time from
# the shipped floors table, never hardcoded, so a floors refresh moves the
# expectation with it and the check fails only when the realized choice
# disagrees with the table that shipped.  The verdict is recorded under its
# own key in the run file rather than as a measured row, so it cannot collide
# with the pinned rows' (geometry, op, size, n_devices) coordinates.
AUTO_CHOICE_GEOMETRY = "cone"
AUTO_CHOICE_SIZE = (512, 448, 384)


def auto_choice_check(config, platform_key):
    """One UNPINNED settle on real devices, judged against the shipped floors.

    Runs in its own worker process like every other job here.  The model is
    built and its sinogram placed through the public entry
    (``prepare_sino_for_devices``), with no pin of either kind, so the settle
    inside it is the same automatic choice a user's first reconstruction
    makes.  The expected count comes from the floors table alone -- the widest
    count ``_widening_floors.admitted`` accepts at this cell's size -- which is
    an independent reading of the same table the policy consults, under the
    assumption that memory is ample.  On the nightly's dedicated node that
    assumption holds by a wide margin at this cell, and a capacity refusal
    would itself be an anomaly; the recorded per-count reasons say which rule
    drove any mismatch.

    ``ok`` is False when the realized count differs from the expected one,
    when the layout did not come from the automatic branch, when the guard was
    disabled in the environment, or when a device-count pin had leaked into
    this process (popped and recorded, since a leaked pin silently un-tests
    exactly the path this check exists to cover).
    """
    import torch
    import mbirtorch  # noqa: F401
    from mbirtorch import _widening_floors as wf

    leaked_pin = os.environ.pop("MBIRTORCH_NUM_DEVICES", None)
    size = tuple(AUTO_CHOICE_SIZE)
    geometry = AUTO_CHOICE_GEOMETRY
    visible = torch.cuda.device_count() if torch.cuda.is_available() else 0
    result = dict(kind="auto_choice", geometry=geometry, size=list(size),
                  visible_devices=int(visible),
                  leaked_env_pin=leaked_pin,
                  guard_enabled=bool(wf.guard_enabled()),
                  floors_stale_note=wf.stale_note())

    elements = wf.sinogram_elements(size)
    admitted = {}
    for n in range(1, max(int(visible), 1) + 1):
        ok, why = wf.admitted(geometry, n, elements)
        admitted[int(n)] = {"admitted": bool(ok), "why": str(why)}
    result["admitted_by_count"] = admitted
    expected = max([n for n, v in admitted.items() if v["admitted"]] or [1])
    result["expected_n_devices"] = int(expected)

    model = make_model(config, geometry, size, platform_key)
    sino_np = make_sinogram(config, size)
    model.prepare_sino_for_devices(sino_np)
    realized = int(model.sino_placement.n_devices)
    result["realized_n_devices"] = realized
    result["layout_is_automatic"] = bool(
        getattr(model, "device_layout_is_automatic", False))
    result["choice_rejections"] = [
        [int(count), str(why)] for count, why
        in (getattr(model, "device_choice_rejections", None) or [])]

    problems = []
    if leaked_pin is not None:
        problems.append(f"MBIRTORCH_NUM_DEVICES={leaked_pin!r} had leaked into "
                        f"this process (popped before the settle)")
    if not result["guard_enabled"]:
        problems.append("the widening guard is disabled in this environment, "
                        "so the floors were never consulted")
    if not result["layout_is_automatic"]:
        problems.append("the settled layout is not marked automatic, so "
                        "something pinned or configured it")
    if realized != expected:
        problems.append(f"the automatic choice took {realized} device(s) where "
                        f"the shipped floors say {expected}")
    result["problems"] = problems
    result["ok"] = not problems
    return result


def run_worker(argv):
    p = argparse.ArgumentParser(description="performance_tracking worker (internal)")
    p.add_argument("--worker", action="store_true")
    p.add_argument("--mode", choices=["setup", "measure", "auto-choice"], required=True)
    p.add_argument("--config", default=None)
    p.add_argument("--platform", required=True)
    p.add_argument("--geometry", default=None)
    p.add_argument("--op", default=None)
    p.add_argument("--size", default=None)
    p.add_argument("--device-counts", type=int, nargs="+", default=None)
    p.add_argument("--out-file", required=True)
    a = p.parse_args(argv)
    assert_platform(a.platform)
    if a.mode == "setup":
        sc.write_worker_result(a.out_file, probe_environment(a.platform))
        return
    config = Config.from_dict(sc.load_yaml(a.config))
    if a.mode == "auto-choice":
        sc.write_worker_result(a.out_file, auto_choice_check(config, a.platform))
        return
    res = measure_cell_group(config, a.geometry, a.op, a.size, a.device_counts,
                             a.platform, a.out_file)
    sc.write_worker_result(a.out_file, res)


# ── Environment identity and provenance ───────────────────────────────────────
def probe_environment(platform_key):
    """What this run measured ON, recorded so a night-to-night shift can be attributed
    to the environment rather than the code.  Runs in a worker, since it imports torch.
    """
    import torch
    import mbirtorch
    info = {
        "platform": platform_key,
        "device_label": (f"GPU ({torch.cuda.get_device_name(0)})"
                         if torch.cuda.is_available()
                         else f"CPU ({_platform.processor() or _platform.machine()})"),
        "max_devices": int(torch.cuda.device_count()) if torch.cuda.is_available() else 1,
        "toolchain": {
            "torch": str(torch.__version__),        # TorchVersion is a str SUBCLASS; yaml.safe_dump refuses it
            "torch_cuda": str(torch.version.cuda) if getattr(torch.version, "cuda", None) else None,
            "python": _platform.python_version(),
            "executable": sys.executable,
            "loaded_modules": os.environ.get("LOADEDMODULES"),
        },
        "packages": sc.installed_packages(),
        "mbirtorch_version": str(mbirtorch.__version__),
    }
    try:
        import triton
        info["toolchain"]["triton"] = str(triton.__version__)
    except Exception:                              # noqa: BLE001
        info["toolchain"]["triton"] = None
    # Which projector bodies this run will actually use.  The nightly measures the
    # SHIPPED configuration, with the kernels default-on, so this is recorded rather than
    # forced.
    try:
        from mbirtorch import kernel_availability as ka
        ok, reason = ka.triton_available()
        info["kernels"] = {"triton_available": bool(ok), "reason": str(reason),
                           "disable_env": os.environ.get("MBIRTORCH_DISABLE_TRITON")}
    except Exception as e:                         # noqa: BLE001
        info["kernels"] = {"triton_available": None, "reason": f"probe failed: {e}"}
    return info


def git_provenance(root):
    """{git_commit, git_commit_date, git_branch, ...} for the mbirtorch checkout."""
    def _g(args):
        try:
            r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                               text=True, timeout=10)
            return r.stdout.strip() if r.returncode == 0 else None
        except Exception:                          # noqa: BLE001
            return None
    dirty = _g(["status", "--porcelain"]) or ""
    dirty_files = [ln[3:].split(" -> ")[-1] for ln in dirty.splitlines()]
    return {"git_commit": _g(["rev-parse", "HEAD"]),
            "git_commit_date": _g(["show", "-s", "--format=%cI", "HEAD"]),
            "git_branch": _g(["rev-parse", "--abbrev-ref", "HEAD"]),
            "git_dirty": bool(dirty),
            "git_dirty_files": dirty_files[:20],
            "git_dirty_code": any(f.startswith("mbirtorch/") for f in dirty_files)}


def _file_tag(prov, fallback_date):
    """Filename tag = ``<commit-UTC-timestamp>_<sha8>``, so each run file is unique per commit and
    sorts chronologically by COMMIT time (not collection time).  Falls back to the collection date
    if commit info is absent (e.g. provenance lookup failed)."""
    import datetime as _dt
    sha = (prov.get("git_commit") or "")[:8]
    stamp = fallback_date
    cd = prov.get("git_commit_date")
    if cd:
        try:
            stamp = _dt.datetime.fromisoformat(cd).astimezone(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        except Exception:
            stamp = fallback_date
    return f"{stamp}_{sha}" if sha else stamp


# ── Record book (best-ever per cell/metric + the commit that set it) ───────────
# Categories tracked, and whether best is the MIN (time/memory) or MAX (speedup).
RECORD_METRICS = {"min_ms": "min", "mem_mb": "min", "speedup": "max"}


def update_records(records, cells, commit, date):
    """Update the cumulative best-per-(cell, metric) record book IN PLACE and annotate cells.

    ``records`` (loaded from records_<plat>.yaml, or {}) maps "geom|op|size|n_dev" -> per-metric
    {value, commit, date, prev}.  For each MEASURED cell, every RECORD_METRICS metric is compared
    against the stored best (min for time/memory, max for speedup): a first-ever value establishes
    a baseline (silent); a value that BEATS the prior best overwrites it (keeping prev) and is a
    "win" — the cell gains a ``new_records`` list naming the won metrics.  The trivial n=1 speedup
    (always 1.0) is excluded.  Returns ``(new_lines, n_baselines)`` for the run summary.
    """
    new_lines, n_baselines = [], 0
    for c in cells:
        if c.get("failed") or c.get("skipped"):
            continue
        key = f"{c['geometry']}|{c['op']}|{c['size']}|{c['n_devices']}"
        rec = records.setdefault(key, {})
        won = []
        for metric, direction in RECORD_METRICS.items():
            if metric not in c:
                continue
            if metric == "speedup" and c["n_devices"] == 1:
                continue   # trivially 1.0 at one device
            val = float(c[metric])
            cur = rec.get(metric)
            if cur is None:
                rec[metric] = {"value": val, "commit": commit, "date": date, "prev": None}
                n_baselines += 1
            elif (val < cur["value"] if direction == "min" else val > cur["value"]):
                new_lines.append(f"  NEW RECORD  {key}  {metric}={val:.4g} "
                                 f"(prev {cur['value']:.4g} @ {(cur.get('commit') or '?')[:8]})")
                rec[metric] = {"value": val, "commit": commit, "date": date,
                               "prev": cur["value"]}
                won.append(metric)
        if won:
            c["new_records"] = won
    return new_lines, n_baselines


# ── Diff + gate (compare a run vs its prior run; classify; set exit code) ──────
def _cell_key(c):
    return f"{c['geometry']}|{c['op']}|{c['size']}|{c['n_devices']}"


def _cell_status(c):
    """ok (measured) / failed / skipped / absent (None)."""
    if c is None:
        return "absent"
    if c.get("failed"):
        return "failed"
    if c.get("skipped"):
        return "skipped"
    return "ok"


def _expected_cells(result):
    """The (geom|op|size|n_dev) keys this run's config was supposed to attempt.

    Restricted per geometry by sharding_by_geom: a geometry that can't shard is only expected at
    n=1, so the gate doesn't flag its (legitimately unmeasured) multi-device cells as 'absent'.
    """
    cfg = result.get("config", {})
    plat = result["platform"]
    geom_sizes = cfg.get("geom_sizes", {}) or {}
    geom_ops = cfg.get("geom_ops", {}) or {}
    default_sizes = cfg.get("sizes", {}).get(plat, [])
    default_ops = cfg.get("ops", [])
    dc = result.get("device_counts", [])
    cap = result.get("sharding_by_geom", {})
    keys = set()
    for g in cfg.get("geometries", []):
        gs = (geom_sizes.get(g, {}) or {}).get(plat) or default_sizes
        sizes = [sc.size_label(s) for s in gs]   # per-geometry sizes (matches the orchestrator)
        ops = geom_ops.get(g) or default_ops     # per-geometry ops (matches the orchestrator)
        g_dc = [1] if cap.get(g) is False else dc
        for op in ops:
            for s in sizes:
                for n in g_dc:
                    keys.add(f"{g}|{op}|{s}|{n}")
    return keys


def _fmt_delta(today, ref, unit=""):
    """'<today> vs <expected> (<+abs>, <+pct>)' — shows BOTH the absolute and the % difference
    so a reader can judge importance (a big % on a tiny absolute is often noise, and vice versa)."""
    d = today - ref
    pct = (d / ref * 100.0) if ref else float("nan")
    return f"{today:g}{unit} vs {ref:g}{unit} expected ({d:+g}{unit}, {pct:+.1f}%)"


def _gate_fingerprint(key, tf, rf, op, lab, config, hard, soft):
    """Correctness gate on the tolerant fingerprint (see §7): exact shape/dtype, robust
    aggregates within rtol (HARD), a few sample deviations allowed (SOFT), new padding leak (HARD).
    Each aggregate finding shows the relative diff vs the tolerance plus the absolute change."""
    if not tf or not rf:
        return
    if tf.get("shape") != rf.get("shape"):
        hard.append(f"[{lab}] {key} fingerprint shape {rf.get('shape')} -> {tf.get('shape')}")
        return
    if tf.get("dtype") != rf.get("dtype"):
        hard.append(f"[{lab}] {key} fingerprint dtype {rf.get('dtype')} -> {tf.get('dtype')}")
    rtol = config.fp_rtol_iter if op in ("vcd_nonconst", "denoise") else config.fp_rtol_single
    for m in ("sum", "mean", "l2norm"):
        rv, tv = rf.get(m), tf.get(m)
        if rv is None or tv is None:
            continue
        reldiff = abs(tv - rv) / (abs(rv) or 1.0)
        if reldiff > rtol:
            hard.append(f"[{lab}] {key} fingerprint {m}: reldiff {reldiff:.2e} > rtol {rtol:g} "
                        f"(Δ {tv - rv:+.3g}; {tv:g} vs {rv:g} expected)")
    rs_, ts_ = rf.get("samples") or [], tf.get("samples") or []
    if rs_ and len(rs_) == len(ts_):
        dev = sum(1 for a, b in zip(rs_, ts_) if abs(b - a) / (abs(a) or 1.0) > rtol)
        if dev > config.k_sample_tol:
            soft.append(f"[{lab}] {key} {dev}/{len(rs_)} fingerprint samples deviate (rtol {rtol:g})")
    if tf.get("padding_zero") is False and rf.get("padding_zero") is not False:
        hard.append(f"[{lab}] {key} padding leak: padding_zero {rf.get('padding_zero')} -> False")


def _memory_is_device_peak(plat):
    """True when this platform's ``mem_mb`` is a per-device ACCELERATOR PEAK, False when it is
    coarse whole-process RSS.  This is what decides whether the memory gate is HARD or SOFT.

    On 'gpu' the reading is ``torch.cuda.max_memory_allocated`` over the row's pinned
    devices, which is a deterministic device-side counter and earns the HARD gate.  On 'cpu'
    it is whole-process resident size, which is too coarse to hard-gate.
    """
    return plat == "gpu"


def _gate_metrics(key, t, r, lab, plat, config, hard, soft):
    """Metric gates for an ok->ok cell.  Structural changes and the correctness fingerprint are
    HARD on every platform.  Of the PERFORMANCE signals, only MEMORY is HARD, and only where
    mem_mb is a device-side peak (see _memory_is_device_peak): peak_bytes_in_use / torch's
    max_memory_allocated are ~deterministic, and memory is what catches the gather-bug class —
    memory that fails to shard.  Where mem_mb is whole-process RSS (coarse) it is SOFT.
    Speedup and absolute time are SOFT on every platform — both derive from timings, which are
    noisy even on GPU (especially small runs).  Every delta shows the value vs expected with BOTH
    the absolute and the percentage difference."""
    pre = f"[{lab}] {key} "
    # memory — HARD where mem_mb is a device peak, SOFT (coarse RSS) elsewhere.
    rm, tm = r.get("mem_mb"), t.get("mem_mb")
    if rm and tm is not None and (tm - rm) / rm * 100.0 > config.mem_hard_pct:
        device_peak = _memory_is_device_peak(plat)
        bucket = hard if device_peak else soft
        cpu_note = "" if device_peak else " [CPU RSS, coarse]"
        win = getattr(config, "mem_gate_window", 1)
        win_note = f" [rolling-min over {win} runs]" if win and win > 1 else ""
        bucket.append(pre + "memory " + _fmt_delta(tm, rm, " MB") + win_note + cpu_note)
    # speedup-ratio drop — SOFT everywhere (ratio of noisy timings).
    rsp, tsp = r.get("speedup"), t.get("speedup")
    if t["n_devices"] > 1 and rsp and tsp is not None and (rsp - tsp) / rsp * 100.0 > config.speedup_warn_pct:
        soft.append(pre + "speedup " + _fmt_delta(tsp, rsp))
    # absolute time — SOFT everywhere.
    rt, tt = r.get("min_ms"), t.get("min_ms")
    if rt and tt is not None and (tt - rt) / rt * 100.0 > config.time_soft_pct:
        soft.append(pre + "time " + _fmt_delta(tt, rt, " ms"))
    # structural — DIRECTION-AWARE.  Losing the sharded path (True->False) or the banded back path
    # (value->None), or a band-count change between two sharded runs, is a HARD regression.  GAINING
    # sharding (False->True) or banding (None->value) is the placement port LANDING — an improvement,
    # recorded SOFT, not gated.  (Without this, the run where sharding lands on a tracked branch flags
    # every newly-sharded cell as a regression — a huge false-positive burst.)
    ts, rs_ = bool(t.get("is_sharded")), bool(r.get("is_sharded"))
    if ts != rs_:
        msg = pre + f"is_sharded {r.get('is_sharded')} -> {t.get('is_sharded')}"
        (hard if (rs_ and not ts) else soft).append(msg + ("" if (rs_ and not ts) else " (gained sharding)"))
    tb, rb = t.get("back_n_bands_per_shard"), r.get("back_n_bands_per_shard")
    if tb != rb and (tb is not None or rb is not None):
        msg = pre + f"back band count {rb} -> {tb}"
        (soft if rb is None else hard).append(msg + (" (banded back-projection landed)" if rb is None else ""))
    _gate_fingerprint(key, t.get("fingerprint"), r.get("fingerprint"), t.get("op", ""),
                      lab, config, hard, soft)


def _compare_cell(key, t, r, lab, plat, expected, oom_gos, config, hard, soft):
    """Classify one cell vs one reference (see plan §10a status transitions)."""
    ts, rs = _cell_status(t), _cell_status(r)
    if rs == "absent":
        soft.append(f"[{lab}] new cell, no baseline (not gated): {key}")
        return
    if ts == "absent":
        gos = tuple(key.split("|")[:3])
        if key not in expected:
            soft.append(f"[{lab}] dropped from sweep: {key}")
        elif gos in oom_gos:
            soft.append(f"[{lab}] {key} not measured (OOM-descent stopped at higher n_dev)")
        else:
            hard.append(f"[{lab}] expected cell vanished (no row/skip/fail): {key}")
        return
    if ts == "failed" and rs == "ok":
        hard.append(f"[{lab}] {key} REGRESSED: was ok, now fails ({str(t.get('error',''))[:50]})")
        return
    if ts == "ok" and rs == "failed":
        soft.append(f"[{lab}] {key} improved: was failing, now ok")
        return
    if ts != "ok" or rs != "ok":   # skip<->fail combos, or unchanged fail/skip (quiet)
        if ts != rs:
            soft.append(f"[{lab}] {key} status {rs} -> {ts}")
        return
    _gate_metrics(key, t, r, lab, plat, config, hard, soft)   # ok -> ok


def gate_run(result, references, config):
    """Compare ``result`` against each (label, ref_result) and return the gate dict.

    Fires on a CHANGE vs the reference (plan §10/§10a): ok->fail / memory / speedup / structural /
    correctness are HARD; absolute time / added-dropped / improvements are SOFT; persistent
    failures are quiet.  Cold start (no usable reference) is all-SOFT, never a fail.
    """
    hard, soft = [], []
    refs = [(lab, r) for lab, r in references if r]
    if not refs:
        return {"result": "warn", "hard": [], "compared_to": [],
                "soft": ["no prior run to compare against (cold start) — nothing gated"]}
    plat = result.get("platform", "")
    expected = _expected_cells(result)
    oom_gos = {(c["geometry"], c["op"], c["size"])
               for c in result["cells"] if c.get("failed") and c.get("oom")}
    today = {_cell_key(c): c for c in result["cells"]}
    for lab, ref in refs:
        refcells = {_cell_key(c): c for c in ref.get("cells", [])}
        for key in sorted(set(today) | set(refcells)):
            _compare_cell(key, today.get(key), refcells.get(key), lab, plat,
                          expected, oom_gos, config, hard, soft)
    # ── DASHBOARD-MARKER CONTRACT (keep in sync) ──────────────────────────────────────────────────
    # Every HARD message above is "[lab] {key} ..." with key = _cell_key(c) = "geom|op|size|ndev".
    # The dashboard (build_dashboard.py `_parse_gate_hard` / `_GATE_CELL_RE`) extracts that cell id to
    # place the red marker on the scaling plot.  A hard message with NO parseable cell is still COUNTED
    # (gate tiles + history) but silently NOT MARKED — a count-vs-marker mismatch.  This should never
    # fire (all keys match the regex); if it does, the gate-string format drifted from the dashboard's
    # — reconcile BOTH sides.  ⚠ If you change _cell_key / the hard-string format, update _GATE_CELL_RE.
    _cell_pat = re.compile(r"[a-z_]+\|[a-z_]+\|\d+x\d+x\d+\|\d+")
    unmarkable = [h for h in hard if not _cell_pat.search(h)]
    if unmarkable:
        print(f"  [gate-marker WARNING] {len(unmarkable)} hard-gate message(s) carry no "
              f"dashboard-parseable cell id (geom|op|size|ndev) — they will be COUNTED but NOT "
              f"marked on the scaling plot.  Reconcile gate_run with build_dashboard.py _GATE_CELL_RE:")
        for h in unmarkable:
            print(f"      {h}")
    return {"result": "fail" if hard else ("warn" if soft else "pass"),
            "hard": hard, "soft": soft, "compared_to": [lab for lab, _ in refs]}


def _find_priors(out_dir, plat, current_tag, n):
    """The ``n`` most-recent run files STRICTLY BEFORE current_tag, NEWEST FIRST (or []).

    Filenames embed the commit-time tag, so a lexicographic sort is chronological by COMMIT time;
    prior[0] is therefore the immediately-preceding commit's run (not just 'yesterday's file').
    """
    import glob
    cur_name = f"regression_{plat}_{current_tag}.yaml"
    # Exclude the sibling *_table.yaml dumps: they match this glob and, since '_table.yaml' sorts AFTER
    # '.yaml', the prior run's table would otherwise be picked as a prior run (gate vs a non-run).
    befores = sorted(nm for nm in (os.path.basename(f)
                     for f in glob.glob(os.path.join(out_dir, f"regression_{plat}_*.yaml")))
                     if nm < cur_name and not nm.endswith("_table.yaml"))
    return [os.path.join(out_dir, nm) for nm in befores[-n:][::-1]]   # newest first


def _find_prior(out_dir, plat, current_tag):
    """Most-recent run file STRICTLY BEFORE current_tag (by name), or None."""
    pri = _find_priors(out_dir, plat, current_tag, 1)
    return pri[0] if pri else None


def _apply_mem_window(result, ref, prior_paths, W):
    """Return (result_win, ref_win): COPIES of tonight's + the reference run with each cell's ``mem_mb``
    replaced by a rolling-MIN over the last ``W`` runs, so a sporadic per-run peak transient on the
    sharded (n>1) path can't false-gate.  MEMORY ONLY — every other field is copied unchanged, so the
    timing/speedup/structural gates are unaffected; and the ORIGINALS are untouched, so the dated YAML
    still records the true single-shot peak.

      current-window (tonight)   = min(tonight, p1..p_{W-1})     [tonight anchors its own window]
      reference-window (=p1)     = min(p1..pW)                   [the window ending at the prior run]

    ``prior_paths`` is newest-first (p1 = immediately-prior).  A real floor shift is still caught, with
    ~(W-1)-run lag; a one-run transient never moves either windowed min.  Missing/failed cells fall back
    to single-shot (min over whatever is present)."""
    prior_mems = {}   # cell_key -> [mem_mb in p1, p2, ... pW]  (newest first, missing runs skipped)
    for p in prior_paths:
        d = sc.load_yaml(p) or {}
        for c in d.get("cells", []):
            m = c.get("mem_mb")
            if m is not None and not c.get("failed") and not c.get("skipped"):
                prior_mems.setdefault(_cell_key(c), []).append(m)

    def win_cells(cells, include_tonight):
        out = []
        for c in cells:
            c2 = dict(c)
            m = c.get("mem_mb")
            if m is not None and not c.get("failed") and not c.get("skipped"):
                pri = prior_mems.get(_cell_key(c), [])
                pool = ([m] + pri[:W - 1]) if include_tonight else pri[:W]
                if pool:
                    c2["mem_mb"] = min(pool)
            out.append(c2)
        return out

    result_win = {**result, "cells": win_cells(result.get("cells", []), include_tonight=True)}
    ref_win = {**ref, "cells": win_cells(ref.get("cells", []), include_tonight=False)}
    return result_win, ref_win


def _print_gate(g):
    print("\n" + "=" * 78)
    print(f"  GATE: {g['result'].upper()}   (vs {', '.join(g['compared_to']) or 'nothing'})")
    print("=" * 78)
    for h in g.get("hard", []):
        print("  HARD  " + h)
    for s in g.get("soft", []):
        print("  warn  " + s)
    if not g.get("hard") and not g.get("soft"):
        print("  no changes vs reference")


def _print_summary(cells):
    """Per (geometry, op): min time (ms) / peak mem (MB) / speedup, for each (size, n_dev)."""
    print("\n" + "=" * 78)
    print("  REGRESSION SUMMARY — min time (ms) / peak mem (MB) / speedup vs fewest devices")
    print("=" * 78)
    groups = OrderedDict()
    for c in cells:
        groups.setdefault((c["geometry"], c["op"]), []).append(c)
    for (g, op), rows in groups.items():
        print(f"\n  {g} | {op}")
        print("  {:<12s}{:>6s}{:>11s}{:>11s}{:>9s}".format(
            "size", "n_dev", "min_ms", "mem_mb", "speedup"))
        for r in sorted(rows, key=lambda r: (r["size"], r["n_devices"])):
            if r.get("skipped"):
                print(f"  {r['size']:<12s}{r['n_devices']:>6d}   [skip] {r['reason']}")
                continue
            if r.get("failed"):
                tag = "OOM" if r.get("oom") else "FAIL"
                print(f"  {r['size']:<12s}{r['n_devices']:>6d}   [{tag}] {str(r.get('error', ''))[:58]}")
                continue
            mark = " !" if r.get("throttled") else ""
            print("  {:<12s}{:>6d}{:>11.1f}{:>11.1f}{:>8.2f}x{}".format(
                r["size"], r["n_devices"], r["min_ms"], r["mem_mb"],
                r.get("speedup", float("nan")), mark))


def _assert_platform_matches_out_dir(plat, out_dir):
    """Abort if the platform THIS process measured on is not the one out_dir claims.

    Two independent platform decisions exist in a nightly run, and they can disagree.
    The SHELL (run_regression.sh -> reg_plat) picks ``results/<plat>/`` from whether
    ``nvidia-smi -L`` succeeds, which reports the hardware being PRESENT.  THIS process
    verifies the same key against ``torch.cuda.is_available()`` in ``assert_platform``,
    which reports torch being able to USE it.

    The two diverged once, on 2026-07-21, in the jax nightly this engine is descended
    from.  nvidia-smi saw the H100s so the run wrote to results/gpu/, but the installed
    CUDA plugin could not initialise under the node's CUDA module, and the framework fell
    back to CPU.  The sweep measured the whole GPU suite ON CPU and filed it as
    ``regression_cpu_*.yaml`` and ``records_cpu.yaml`` inside ``results/gpu/``, which is a
    records file the dashboard never reads.  Tests passed, the engine exited 0, the failure
    mail stayed quiet, and the only symptom was GPU charts silently not updating.

    A CPU-measured run must never be filed as a GPU run.  Fail loudly instead: a crashed
    engine is alerted on, a silently mislabelled one is not.  Manual runs
    (``results/manual/<tag>/``) make no platform claim and are exempt.
    """
    parts = os.path.normpath(out_dir).split(os.sep)
    claimed = next((p for p in reversed(parts) if p in ("gpu", "cpu")), None)
    if claimed is None or claimed == plat:
        return
    raise RuntimeError(
        "PLATFORM MISMATCH: out_dir claims '{claimed}' but this run is '{plat}'.\n"
        "  out_dir: {out_dir}\n"
        "  Measuring on {plat} and filing under {claimed}/ would write records_{plat}.yaml\n"
        "  into the {claimed} tree, where the dashboard does not read it -- the charts would\n"
        "  go quiet with no other symptom.  Aborting instead.\n"
        "  Most likely cause: torch could not initialise CUDA and fell back to CPU.  The usual\n"
        "  culprit is TORCH_INDEX_URL_gpu in run_configs.env not matching the CUDA module the\n"
        "  node loads (PREAMBLE_FILE -> `module load cuda`).".format(
            claimed=claimed, plat=plat, out_dir=out_dir))


# ── Orchestrator ──────────────────────────────────────────────────────────────
def run(config, platform_key):
    """Sweep, gate, and write the dated YAML + companions."""
    script = os.path.abspath(__file__)
    # Second half of the platform guard.  assert_platform (in each worker) checks the declared
    # key against the hardware; this checks it against the directory the run will be filed in.
    _assert_platform_matches_out_dir(platform_key, config.out_dir)
    os.makedirs(config.out_dir, exist_ok=True)
    worker_env = {"PYTHONPATH": os.pathsep.join(
        [p for p in (config.lib_root, os.environ.get("PYTHONPATH")) if p])}

    print("=" * 72)
    print("  performance_tracking — mbirtorch regression sweep")
    print(f"  lib_root (under test): {config.lib_root}")
    print(f"  out_dir:               {config.out_dir}")
    print(f"  platform / date / tag: {platform_key} / {config.date} / {config.run_tag or '-'}")
    print("=" * 72)

    setup, rc = sc.run_worker(script, ["--worker", "--mode", "setup",
                                       "--platform", platform_key], extra_env=worker_env)
    if setup is None:
        print(f"  ERROR: setup worker produced no result (rc={rc}); aborting.")
        return None
    max_dev = int(setup.get("max_devices") or 1)
    print(f"  device: {setup['device_label']}   visible devices: {max_dev}")
    print(f"  torch {setup['toolchain']['torch']} · kernels: {setup.get('kernels')}")

    device_counts = [n for n in config.device_counts if n <= max_dev]
    if not device_counts:
        print(f"  ERROR: no requested device count fits {max_dev} visible device(s); aborting.")
        return None

    fd, cfg_path = tempfile.mkstemp(suffix=".yaml", prefix="torch_cfg_")
    os.close(fd)
    sc.save_yaml(cfg_path, config.to_dict())

    # The automatic-device-choice check runs once, before the sweep, wherever a
    # choice exists (two or more visible devices).  Every measured row below
    # pins its count, so this is the only place the automatic path runs.  The
    # worker gets NO device-count pin.
    auto_choice = None
    if platform_key == "gpu" and max_dev >= 2:
        print("\n=== automatic device choice (unpinned settle, "
              f"{AUTO_CHOICE_GEOMETRY} {sc.size_label(AUTO_CHOICE_SIZE)}) ===")
        auto_choice, _rc = sc.run_worker(
            script, ["--worker", "--mode", "auto-choice", "--config", cfg_path,
                     "--platform", platform_key], extra_env=worker_env)
        if auto_choice is None:
            auto_choice = {"kind": "auto_choice", "ok": False,
                           "problems": ["the auto-choice worker produced no "
                                        "result"]}
        if auto_choice.get("ok"):
            print(f"  ok: chose {auto_choice.get('realized_n_devices')} "
                  f"device(s), as the shipped floors say "
                  f"(expected {auto_choice.get('expected_n_devices')}, "
                  f"{auto_choice.get('visible_devices')} visible)")
        else:
            print("  AUTO-CHOICE MISMATCH:")
            for problem in auto_choice.get("problems") or []:
                print(f"    {problem}")
            for count, why in auto_choice.get("choice_rejections") or []:
                print(f"    count {count} rejected: {why}")
        if auto_choice.get("floors_stale_note"):
            print(f"  note: {auto_choice['floors_stale_note']}")
    else:
        auto_choice = {"kind": "auto_choice",
                       "skipped": ("single-device night: no choice exists"
                                   if platform_key == "gpu" else
                                   "cpu platform: the automatic choice is a "
                                   "CUDA path")}
        print(f"\n  automatic device choice check skipped: "
              f"{auto_choice['skipped']}")

    cells = []
    swept_counts = set()
    for geometry in config.geometries:
        gs = (config.geom_sizes.get(geometry, {}) or {}).get(platform_key) \
            or config.sizes[platform_key]
        size_labels = [sc.size_label(s) for s in gs]
        for op in (config.geom_ops.get(geometry) or config.ops):
            for label in size_labels:
                # Per-(geometry, size) counts: only the MULTI_DEVICE_SIZE_LABELS cells
                # sweep n>1; everything else, and the whole denoiser, stays n=1.
                gdc = cell_device_counts(geometry, label, device_counts)
                swept_counts.update(gdc)
                print(f"\n=== {geometry} | {op} | {label} @ n={gdc} ===")
                # Second, independent pin layer: the process-wide
                # env pin covers any model a code path constructs WITHOUT an explicit
                # configure_devices call (explicit pins always win over it).  It is a
                # single value per process, so it is exportable only when this worker
                # sweeps exactly one count — true for every n=1 row today.  The n>1
                # increment sweeps several counts per worker and relies on the explicit
                # pin + realized-list assertion alone.
                cell_env = (dict(worker_env, MBIRTORCH_NUM_DEVICES=str(gdc[0]))
                            if len(gdc) == 1 else worker_env)
                args = ["--worker", "--mode", "measure", "--config", cfg_path,
                        "--platform", platform_key, "--geometry", geometry, "--op", op,
                        "--size", label, "--device-counts", *[str(n) for n in gdc]]
                res, _rc = sc.run_worker(script, args, extra_env=cell_env)
                if not res:
                    print(f"  (no result for {geometry}/{op}/{label})")
                    continue
                rows = res.get("rows") or []
                sc.annotate_speedups(rows)
                cells.extend(rows)
                for f in (res.get("failures") or []):
                    cells.append({"geometry": geometry, "op": op, "size": label,
                                  "n_devices": f["n_devices"], "failed": True,
                                  "oom": bool(f.get("oom")), "error": f.get("error")})
    os.path.exists(cfg_path) and os.remove(cfg_path)

    prov = git_provenance(config.lib_root)
    if prov.get("git_branch") in (None, "", "HEAD") and config.run_tag:
        prov["git_branch"] = config.run_tag
    file_tag = _file_tag(prov, config.date)   # COMMIT-time tag: one file per commit,
    #                                              sorts chronologically, overwrites on re-measure

    records_path = os.path.join(config.out_dir, f"records_{platform_key}.yaml")
    records = (sc.load_yaml(records_path) or {}) if os.path.exists(records_path) else {}
    new_lines, n_baselines = update_records(records, cells, prov.get("git_commit") or "?",
                                               config.date)
    sc.save_yaml(records_path, records)

    cfg_dict = config.to_dict()
    cfg_dict["backend"] = "torch"
    result = {
        "kind": "regression", "date": config.date, "platform": platform_key,
        # mbirtorch has no per-geometry sharding capability probe: every geometry either
        # supports placement or, for the denoiser, is deliberately held at one device.
        "sharding_by_geom": {g: (g != "denoiser") for g in config.geometries},
        "device_label": setup["device_label"], **prov,
        "mbirtorch_version": f"mbirtorch {setup['mbirtorch_version']}",
        "toolchain": setup["toolchain"],
        "packages": setup.get("packages") or {},
        "kernels": setup.get("kernels"),
        "mem_kind": "gpu_peak_per_device" if platform_key == "gpu" else "cpu_rss",
        "dep_gen": 0, "run_reason": "commit", "torch_available": None,
        "measured_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": cfg_dict, "device_counts": sorted(swept_counts), "cells": cells,
        "policy": {},
        "auto_choice": auto_choice,
    }

    gate_dict = None
    if config.compare_to_prior:
        W = max(1, int(getattr(config, "mem_gate_window", 1)))
        priors = _find_priors(config.out_dir, platform_key, file_tag, W)
        if priors:
            ref = sc.load_yaml(priors[0]) or {}
            gate_result, gate_ref = ((result, ref) if W <= 1
                                     else _apply_mem_window(result, ref, priors, W))
            refs = [(f"prior:{os.path.basename(priors[0])}", gate_ref)]
            gate_dict = gate_run(gate_result, refs, config)
        else:
            gate_dict = gate_run(result, [], config)   # cold start -> all-SOFT
        result["gate"] = gate_dict

    # A failed auto-choice check gates HARD, cold start included: its
    # expectation comes from the shipped floors table, not from a prior run,
    # so there is nothing to warm up.  A skipped check gates nothing.
    if auto_choice and not auto_choice.get("skipped") and not auto_choice.get("ok"):
        line = ("[auto-choice] the automatic device choice disagrees with the "
                "shipped floors: " + "; ".join(auto_choice.get("problems")
                                               or ["no detail recorded"]))
        if gate_dict is None:
            gate_dict = {"result": "fail", "hard": [line], "soft": [],
                         "compared_to": []}
        else:
            gate_dict["hard"] = list(gate_dict.get("hard") or []) + [line]
            gate_dict["result"] = "fail"
        result["gate"] = gate_dict

    out_path = os.path.join(config.out_dir, f"regression_{platform_key}_{file_tag}.yaml")
    sc.save_yaml(out_path, result)
    try:
        import regression_to_table
        regression_to_table.write_table(regression_to_table.load_yaml(out_path),
                                        os.path.splitext(out_path)[0] + "_table.yaml")
    except Exception as e:                         # noqa: BLE001
        print(f"[warn] companion _table.yaml not written: {e}")

    _print_summary(cells)
    if new_lines:
        print(f"\n  {len(new_lines)} NEW RECORD(S) this run:")
        for line in new_lines:
            print(line)
    elif n_baselines:
        print(f"\n  established {n_baselines} baseline record(s) (first run for these cells)")
    if gate_dict:
        _print_gate(gate_dict)
    print(f"\nOutput written to: {out_path}")
    print(f"Record book:       {records_path}")
    return result


def write_tests_log(lib_root, out_dir, platform_key, date):
    """Run the mbirtorch suite and capture its output beside the run.

    The suite carries the cross-framework coverage: test_vs_goldens.py holds the
    mbirtorch-against-mbirjax value check.  That is why the nightly itself has no
    cross-framework column.
    """
    tests_path = os.path.join(out_dir, f"tests_{platform_key}_{date}.txt")
    nproc = "4" if platform_key == "gpu" else "8"
    runner = os.path.join(lib_root, "dev_scripts", "run_tests.sh")
    env = {**os.environ, "PYTEST_NPROC": nproc}
    if os.path.isfile(runner):
        # run_tests.sh uses a path RELATIVE to dev_scripts/, so it must run from there.
        proc = subprocess.run(["bash", "run_tests.sh"], cwd=os.path.dirname(runner),
                              capture_output=True, text=True, env=env)
    else:
        proc = subprocess.run([sys.executable, "-m", "pytest", "tests", "-ra", "-n", nproc],
                              cwd=lib_root, capture_output=True, text=True, env=env)
    with open(tests_path, "w") as f:
        f.write(proc.stdout + proc.stderr)
    print(f"wrote {tests_path}")
    return tests_path


def main():
    platform_key = os.environ.get("REG_PLATFORM")
    lib_root = os.environ.get("REG_LIB_ROOT")
    out_dir = os.environ.get("REG_OUT_DIR")
    for name, val in (("REG_PLATFORM", platform_key),
                      ("REG_LIB_ROOT", lib_root),
                      ("REG_OUT_DIR", out_dir)):
        if not val:
            raise SystemExit(f"performance_tracking: required env var {name} is not set")
    assert_no_calibration()
    date = os.environ.get("REG_DATE") or datetime.datetime.now().strftime("%Y%m%d")
    counts = [int(x) for x in (os.environ.get("REG_DEVICE_COUNTS") or "1").split()]
    config = build_config(platform_key, out_dir, date,
                          os.environ.get("REG_RUN_TAG", ""), lib_root, counts,
                          gate=os.environ.get("REG_GATE", "1") == "1")
    if os.environ.get("REG_SMOKE") == "1":
        # Fast plumbing check (NOT a measurement): one tiny cell, end to end.
        config.geometries = ["parallel"]
        config.ops = ["back"]
        config.sizes = {platform_key: [[40, 40, 48]]}
        config.geom_sizes = {}
        config.device_counts = [1]

    result = run(config, platform_key)
    if result is None:
        raise SystemExit(2)
    # The nightly wrapper owns the test step (live output, crash detection, alert mail), so it
    # exports REG_SKIP_TESTS=1; a standalone/manual invocation still runs the suite here.
    if os.environ.get("REG_SMOKE") != "1" and os.environ.get("REG_SKIP_TESTS") != "1":
        write_tests_log(lib_root, out_dir, platform_key, date)
    if config.gate and (result.get("gate") or {}).get("result") == "fail":
        raise SystemExit(1)      # HARD regression -> the wrapper turns this into an alert


if __name__ == "__main__":
    if "--worker" in sys.argv:
        run_worker(sys.argv[1:])
    else:
        main()
