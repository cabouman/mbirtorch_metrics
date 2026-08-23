#!/usr/bin/env python3
"""Build a self-contained HTML dashboard from the mbirtorch_metrics YAML time series.

This is a *reader/generator*: it walks the metrics repo's result files, parses them
into a compact JSON blob, and inlines that JSON — together with the page's CSS, JS,
and a vendored copy of the uPlot charting library — into a single static
``index.html``.  The output opens straight from ``file://`` (or via GitHub Pages),
needs no server, and makes no network calls.

What it reads (all already produced by the nightly engine):
  results/<plat>/<branch>/regression_<plat>_<date>.yaml   the per-run cells + gate
  results/<plat>/<branch>/records_<plat>.yaml             best-ever per cell+metric (records panel)
  results/<plat>/<branch>/tests_<plat>_<date>.txt         pytest summary line
The "compare against" overlays (main / prerelease / prior run / best-ever) are all derived from the
runs above — there is no separate reference file.

Run it on demand to refresh the page::

    cd tooling/dashboard
    python build_dashboard.py        # -> <repo>/dashboard/index.html

There are no command-line arguments by design; the few knobs live in the CONFIG
block below so a run is reproducible from the checked-in source.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import re
import subprocess
from pathlib import Path

import yaml

# libyaml-backed loader when available: the pure-Python SafeLoader dominated the whole
# build (~95% of wall time parsing the regression YAMLs).  CSafeLoader parses the same
# documents ~15-30x faster; the fallback keeps environments without libyaml working.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def _yaml_load(text):
    return yaml.load(text, Loader=_YAML_LOADER)


# --------------------------------------------------------------------------- #
# CONFIG — edit here, not via the command line.                               #
# --------------------------------------------------------------------------- #
# Repo root is inferred as two levels up from this file (tooling/dashboard/ -> repo).
HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
OUT_PATH = REPO_ROOT / "dashboard" / "index.html"

# Optional filters; None means "everything discovered under results/".
ONLY_PLATFORMS: list[str] | None = None      # e.g. ["cpu", "gpu"]
ONLY_BRANCHES: list[str] | None = None        # e.g. ["greg/conebeam_sharding"] (display names)

# Fingerprint fields surfaced in the per-cell drill-down (samples dropped to keep
# the embedded JSON lean as the history grows — these five summarise the array).
_FP_KEYS = ("sum", "mean", "l2norm", "min", "max")


def _repo_url() -> str | None:
    """Browsable URL of this repo's `origin` remote (for the title link), or None if unavailable.

    Normalises both forms git emits: ``git@github.com:org/repo.git`` and
    ``https://github.com/org/repo.git`` -> ``https://github.com/org/repo``.
    """
    try:
        url = subprocess.run(["git", "-C", str(REPO_ROOT), "remote", "get-url", "origin"],
                             capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return None
    if not url:
        return None
    if url.startswith("git@"):                       # git@host:org/repo(.git)
        url = "https://" + url[4:].replace(":", "/", 1)
    return url[:-4] if url.endswith(".git") else url


def _generated_stamp() -> str:
    """Build time in US Eastern — EST or EDT per the date (%Z resolves it).  This is correct wherever
    the build runs (local or the UTC Pages runner), since now(tz) gives that zone's wall clock.  Falls
    back to UTC if the system has no tz database."""
    try:
        from zoneinfo import ZoneInfo
        return _dt.datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M %Z")
    except Exception:
        return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# --------------------------------------------------------------------------- #
# Parsing helpers                                                             #
# --------------------------------------------------------------------------- #
def _slim_fingerprint(fp: dict | None) -> dict | None:
    """Keep the interpretable scalar summary; drop the raw sample list."""
    if not fp:
        return None
    out = {k: fp.get(k) for k in _FP_KEYS}
    if fp.get("shape") is not None:
        out["shape"] = fp["shape"]
    if fp.get("dtype") is not None:
        out["dtype"] = fp["dtype"]
    return out


def _slim_gpu_health(gs) -> list | None:
    """Per-GPU clocks/temps/throttle for the page — but ONLY for cells worth flagging (any GPU at or
    over a warm threshold, or a throttle reason active); cool cells carry nothing, keeping the
    embedded JSON lean.  Short keys: i=index, t=core°C, mt=HBM°C, sm=SM MHz, mem=mem MHz, thr=reasons.
    (A hot GPU drags the slowest-device-gated multi-GPU timing, so this is what tells a thermal
    slowdown apart from a real regression.)"""
    if not gs:
        return None
    if not any((g.get("temp_c") or 0) >= 80 or (g.get("mem_temp_c") or 0) >= 90 or g.get("throttle")
               for g in gs):
        return None
    out = []
    for g in gs:
        d = {"i": g.get("index"), "t": g.get("temp_c"), "sm": g.get("sm_mhz")}
        if g.get("mem_temp_c") is not None:
            d["mt"] = g["mem_temp_c"]
        if g.get("mem_mhz") is not None:
            d["mem"] = g["mem_mhz"]
        if g.get("throttle"):
            d["thr"] = g["throttle"]
        out.append(d)
    return out


def _slim_cell(c: dict) -> dict:
    """Reduce a raw regression cell to the fields the page needs."""
    return {
        "geom": c.get("geometry"),
        "op": c.get("op"),
        "size": c.get("size"),
        "ndev": c.get("n_devices"),
        "min_ms": c.get("min_ms"),
        "mem_mb": c.get("mem_mb"),
        "speedup": c.get("speedup"),
        "is_sharded": c.get("is_sharded"),
        "throttled": c.get("throttled"),
        "gpu": _slim_gpu_health(c.get("gpu_health")),
        "failed": bool(c.get("failed", False)),
        "oom": bool(c.get("oom", False)),
        "error": c.get("error"),
        "fp": _slim_fingerprint(c.get("fingerprint")),
    }


_TESTS_RE = {
    "passed": re.compile(r"(\d+)\s+passed"),
    "failed": re.compile(r"(\d+)\s+failed"),
    "skipped": re.compile(r"(\d+)\s+skipped"),
}


def _parse_tests(txt_path: Path) -> dict | None:
    """Pull pass/fail/skip counts and the failing node-ids from a pytest log."""
    if not txt_path.exists():
        return None
    text = txt_path.read_text(errors="replace")
    out = {}
    for kind, rx in _TESTS_RE.items():
        m = rx.search(text)
        out[kind] = int(m.group(1)) if m else 0
    # The "short test summary info" block lists one `FAILED <nodeid>[ - <err>]`
    # line per failure; capture the node-ids for the tile drill-down.
    failures = []
    for line in text.splitlines():
        if line.startswith("FAILED "):
            failures.append(line[len("FAILED "):].split(" - ", 1)[0].strip())
    if not (out["passed"] or out["failed"] or out["skipped"]):
        return None
    out["failures"] = failures
    return out


_GATE_BASIS_RE = re.compile(r"^\[([^\]]+)\]\s*")   # whole basis incl. "prior:regression_<plat>_<ts>_<sha>.yaml"
# Extracts the cell id "geom|op|size|ndev" from a hard-gate string so the dashboard can place the red
# marker on the scaling plot.  ⚠ KEEP IN SYNC with the engine's gate-string format (performance_tracking.py
# `_cell_key` / `gate_run`, which self-warns if a hard message ever stops matching this pattern).
_GATE_CELL_RE = re.compile(r"([a-z_]+\|[a-z_]+\|\d+x\d+x\d+\|\d+)")


def _hard_kind(text: str) -> str:
    """Classify a hard-gate string as 'correctness' or 'perf' (the severity split — see the
    correctness-gating design note).  Correctness = the fingerprint divergence / padding-leak gates;
    everything else (memory, structural is_sharded/band-count, ok->fail, absent) is perf.  The engine's
    correctness strings always say 'fingerprint …' or 'padding leak …' (performance_tracking.py
    `_gate_fingerprint`); keep this in sync if those phrasings change."""
    t = text.lower()
    return "correctness" if ("fingerprint" in t or "padding leak" in t) else "perf"


def _parse_gate_hard(items) -> list[dict]:
    """Split each hard-gate string into {basis, cell, text, kind}.

    The engine prefixes each entry with its comparison basis, e.g.
    ``[prior:regression_gpu_...yaml] cone|back|512x448x384|1 memory ...`` — so the dashboard can
    show what it was compared against and mark the offending cell on the plot.  ``kind`` carries the
    correctness/perf severity split so the dashboard can surface correctness on its own.
    """
    out = []
    for s in items or []:
        s = str(s)
        mb = _GATE_BASIS_RE.match(s)
        mc = _GATE_CELL_RE.search(s)
        text = _GATE_BASIS_RE.sub("", s).strip()          # basis stripped -> "<cell> <discrepancy>"
        cell = mc.group(1) if mc else None
        # ``detail`` is just the discrepancy (cell id stripped too) so the UI can group hits per cell.
        detail = text[len(cell):].strip() if (cell and text.startswith(cell)) else text
        out.append({
            "basis": mb.group(1) if mb else None,
            "cell": cell,
            "text": text,
            "detail": detail,
            "kind": _hard_kind(s),
        })
    return out


# --------------------------------------------------------------------------- #
# Correctness analyzer (P2): cross-device + vs-main, computed over the corpus   #
# --------------------------------------------------------------------------- #
# Tolerances are calibrated from the corpus dry-run: the cross-device noise floor is ~1e-6 (sharding is
# value-preserving), and vs-main's meaningful reorders sit at 4-8e-5 while value-preserving float drift
# sits at ~1e-6 — so 1e-5 cleanly separates "real change" from "noise".  VCD is looser (seed-dependent).
XDEV_RTOL = 1e-5            # cross-device (n>1 vs n=1, same build): a few x the ~1e-6 floor
VSMAIN_RTOL_SINGLE = 1e-5   # vs-main, single-shot ops (direct_filter / forward / back)
VSMAIN_RTOL_ITER = 1e-4     # vs-main, the iterated vcd_nonconst
# cross-platform (CPU vs GPU, same commit) — calibrated 2026-06-22 from 42 shared cells over 8 commits:
# the CPU<->GPU fingerprint reldiff floor is <=4.2e-6 (single-op) and <=5.5e-6 (vcd_nonconst), only ~5x
# the cross-device floor — so 1e-4 sits ~20x above it (a real backend divergence would be far larger).
# The build prints the live cross-platform floor (see _correctness_summary); re-tighten if it grows.
VSPLAT_RTOL_SINGLE = 1e-4
VSPLAT_RTOL_ITER = 1e-4
_FP_FIELDS = ("sum", "mean", "l2norm", "shape", "dtype", "padding_zero")


def _commit_minute(run: dict) -> str:
    cd = run.get("commit_date")
    return cd[:16].replace("T", " ") if cd else (run.get("date") or "?")


def _degenerate(fp: dict) -> bool:
    """A reference fingerprint that is all-zero / no-op (e.g. main's pre-fix multiaxis direct_filter):
    not a usable baseline, so vs-main skips it rather than reporting a meaningless reldiff."""
    return not fp.get("l2norm") and not fp.get("sum")


def _fp_discrepancies(t: dict, r: dict, rtol: float) -> list[str]:
    """Human discrepancy strings where ``t`` diverges from reference ``r`` beyond ``rtol`` (structural
    shape/dtype changes are always reported)."""
    if t.get("shape") != r.get("shape"):
        return [f"shape {r.get('shape')} -> {t.get('shape')}"]
    out = []
    if t.get("dtype") != r.get("dtype"):
        out.append(f"dtype {r.get('dtype')} -> {t.get('dtype')}")
    for m in ("sum", "mean", "l2norm"):
        rv, tv = r.get(m), t.get(m)
        if rv is None or tv is None:
            continue
        rd = abs(tv - rv) / (abs(rv) or 1.0)
        if rd > rtol:
            out.append(f"{m}: reldiff {rd:.2e} > rtol {rtol:g} (Δ {tv - rv:+.3g}; {tv:g} vs {rv:g} expected)")
    return out


def _fp_reldiff(t: dict, r: dict):
    """Max relative diff over {sum, mean, l2norm} (the scalar the gates threshold), or None if
    uncomparable; inf on a shape mismatch.  Used to report the cross-device noise floor for tuning."""
    if t.get("shape") != r.get("shape"):
        return float("inf")
    vals = [abs(tv - rv) / (abs(rv) or 1.0)
            for m in ("sum", "mean", "l2norm")
            for rv, tv in [(r.get(m), t.get(m))] if rv is not None and tv is not None]
    return max(vals) if vals else None


def _fmt_cell(cell: str) -> str:
    """'geom|op|size|ndev' -> 'geom, op, size, n_devices=N' (the human config label)."""
    p = (cell or "").split("|")
    return f"{p[0]}, {p[1]}, {p[2]}, n_devices={p[3]}" if len(p) == 4 else (cell or "")


def _read_cleared_through(root) -> str | None:
    """The correctness ack watermark (``cleared_through``) from ``<root>/results/correctness_acks.yaml``,
    or None.  Pulled out so the status view can also read it from THIS checkout (where the user runs the
    clear script) even when summarizing a results repo that hasn't pulled the acknowledgment yet."""
    from pathlib import Path
    p = Path(root) / "results" / "correctness_acks.yaml"
    if not p.exists():
        return None
    v = (_yaml_load(p.read_text()) or {}).get("cleared_through")
    return str(v) if v else None   # YAML parses an ISO date to datetime.date; keep it a string ("YYYY-MM-DD")


def _analyze_correctness(runs: list[dict]) -> dict:
    """Annotate each run with a unified ``correctness`` finding list from THREE references (design note
    D2): the prior run on this branch (from the engine's gate.hard), single-device n=1 within the run
    (cross-device), and the latest main run on the same platform (vs-main).  Each finding is
    ``{reference, cell, basis, discrepancies}``.  Reads the per-run ``_fps`` fingerprint index."""
    def _t(r): return r.get("commit_date") or r.get("date") or ""
    main_latest: dict[str, dict] = {}
    by_bc: dict = {}   # (branch, commit_full) -> {platform: run}, for the CPU<->GPU cross-platform match
    for r in runs:
        if r["branch"] == "main":
            cur = main_latest.get(r["platform"])
            if cur is None or _t(r) > _t(cur):
                main_latest[r["platform"]] = r
        if r.get("commit_full"):
            by_bc.setdefault((r["branch"], r["commit_full"]), {})[r["platform"]] = r

    def _prior_label(fn, plat):
        m = re.search(r"_([0-9a-f]{7,40})\.ya?ml$", fn or "")
        sha = m.group(1) if m else None
        if sha:
            for x in runs:
                if x["platform"] == plat and (x.get("commit_full") or "").startswith(sha):
                    return f"{plat.upper()} · {x['commit']} · {_commit_minute(x)}"
            return f"{plat.upper()} · {sha[:10]}"
        return fn or "?"

    xdev_diffs = []   # every cross-device reldiff (the implementation-noise floor, for tuning)
    xplat_diffs = []  # every CPU<->GPU shared-cell reldiff (the cross-platform floor, for tuning)
    for r in runs:
        plat, fps, findings = r["platform"], (r.get("_fps") or {}), []
        # prior run — fold the engine's gate.hard correctness hits into the unified list (one per cell).
        prior_basis = _prior_label((r["gate"].get("compared_to") or [None])[0], plat) if r["gate"].get("compared_to") else "prior run"
        prior_by_cell: dict = {}
        for h in r["gate"]["hard"]:
            if h.get("kind") == "correctness":
                prior_by_cell.setdefault(h.get("cell") or "—", []).append(h.get("detail") or h.get("text"))
        for cell, discr in prior_by_cell.items():
            findings.append({"reference": "prior", "cell": cell, "basis": prior_basis, "discrepancies": discr})
        # cross-device — n>1 vs n=1 within this run (same build, only the device mesh differs).
        by: dict = {}
        for key, fp in fps.items():
            g, op, sz, nd = key.split("|")
            by.setdefault((g, op, sz), {})[int(nd)] = fp
        for (g, op, sz), d in by.items():
            base = d.get(1)
            if base is None or _degenerate(base):
                continue
            for nd in sorted(k for k in d if k != 1):
                rd = _fp_reldiff(d[nd], base)
                if rd is not None and rd != float("inf"):
                    xdev_diffs.append(rd)
                discr = _fp_discrepancies(d[nd], base, XDEV_RTOL)
                if discr:
                    findings.append({"reference": "cross_device", "cell": f"{g}|{op}|{sz}|{nd}",
                                     "basis": f"{plat.upper()} · n=1 (same run)", "discrepancies": discr})
        # vs-main — each cell vs the latest main run's same cell (skip degenerate/absent main baselines).
        mref = main_latest.get(plat)
        if mref is not None and r["branch"] != "main":
            mfps, mlabel = (mref.get("_fps") or {}), f"MAIN · {mref['commit']} · {_commit_minute(mref)}"
            for key, fp in fps.items():
                rfp = mfps.get(key)
                if rfp is None or _degenerate(rfp):
                    continue
                op = key.split("|")[1]
                rtol = VSMAIN_RTOL_ITER if op in ("vcd_nonconst", "denoise") else VSMAIN_RTOL_SINGLE
                discr = _fp_discrepancies(fp, rfp, rtol)
                if discr:
                    findings.append({"reference": "vs_main", "cell": key, "basis": mlabel, "discrepancies": discr})
        # cross-platform — this run's cells vs the OTHER platform's run at the same (branch, commit).
        # Only fires where a cell is measured on BOTH platforms (a shared size — none yet; see Config).
        partners = by_bc.get((r["branch"], r.get("commit_full")), {})
        other = next((p for p in partners if p != plat), None)
        if other is not None:
            ofps, olabel = (partners[other].get("_fps") or {}), f"{other.upper()} · {partners[other]['commit']} · {_commit_minute(partners[other])}"
            for key, fp in fps.items():
                rfp = ofps.get(key)
                if rfp is None or _degenerate(rfp):
                    continue
                op = key.split("|")[1]
                if plat < other:        # the comparison is symmetric — count each CPU<->GPU pair once
                    rd = _fp_reldiff(fp, rfp)
                    if rd is not None and rd != float("inf"):
                        xplat_diffs.append(rd)
                rtol = VSPLAT_RTOL_ITER if op in ("vcd_nonconst", "denoise") else VSPLAT_RTOL_SINGLE
                discr = _fp_discrepancies(fp, rfp, rtol)
                if discr:
                    findings.append({"reference": "cross_platform", "cell": key, "basis": olabel, "discrepancies": discr})
        r["correctness"] = findings
    return {"xdev_floor": max(xdev_diffs) if xdev_diffs else None, "xdev_n": len(xdev_diffs),
            "xplat_floor": max(xplat_diffs) if xplat_diffs else None, "xplat_n": len(xplat_diffs)}


def _pkg_diff(old: dict, new: dict) -> list[dict]:
    """``[{name, from, to}]`` for every package added/removed/changed between two ``{name: version}`` maps
    (sorted by name).  Empty when either side is missing — nothing meaningful to diff (e.g. a run measured
    before ``packages`` was recorded)."""
    if not old or not new:
        return []
    out = []
    for n in sorted(set(old) | set(new)):
        a, b = old.get(n), new.get(n)
        if a != b:
            out.append({"name": n, "from": a, "to": b})
    return out


def _annotate_dep_info(runs: list[dict]) -> None:
    """Attach a compact ``dep_info`` to each dep_gen>0 run: what its dependency set changed RELATIVE to the
    previous generation of the same commit (torch version delta + the per-package diff).  This is what the
    dashboard's dep-change marker renders on hover.  Reads the private ``_pkgs`` map (dropped from the JSON
    later); the torch delta comes from ``toolchain`` and is present even when ``_pkgs`` isn't."""
    by_commit: dict = {}   # (platform, branch, commit_full) -> {dep_gen: run}
    for r in runs:
        key = (r["platform"], r["branch"], r.get("commit_full") or "")
        by_commit.setdefault(key, {})[r.get("dep_gen") or 0] = r
    for r in runs:
        g = r.get("dep_gen") or 0
        if g <= 0:
            continue
        gens = by_commit.get((r["platform"], r["branch"], r.get("commit_full") or ""), {})
        prior = next((gens[pg] for pg in sorted((k for k in gens if k < g), reverse=True)), None)
        changes = _pkg_diff((prior or {}).get("_pkgs") or {}, r.get("_pkgs") or {})
        r["dep_info"] = {
            "reason": r.get("run_reason") or "commit",
            "gen": g,
            "torch_from": (prior or {}).get("torch"),
            "torch_to": r.get("torch"),
            "torch_available": r.get("torch_available"),   # PyPI-latest at run time: if it differs
            #   from torch_to, the tooltip shows "available X, installed Y" (seen-but-not-installed)
            "pkg_changes": changes[:20],   # cap so a first-ever full env doesn't bloat the tooltip
            "pkg_n": len(changes),
        }


_POLICY_FIELDS = ("partition_sequence", "granularity", "max_iterations_default", "axial_extension")


def _annotate_config_changes(runs: list[dict]) -> None:
    """Attach ``cfg_info`` to any run whose recorded library policy defaults differ from the
    PREVIOUS run of the same (platform, branch).  This is what the dashboard's config-change
    marker renders: a default change reads as a perf/memory step in every cell (a real case:
    partition_sequence [4,7] -> [0,2,4,6,7] via a merge = +12-40% memory in vcd_nonconst), and
    the marker makes the step self-explaining on the chart.  Runs without a recorded policy
    (older engine versions) are skipped — no diff is claimed across unknown states."""
    by_bp: dict = {}
    for r in runs:   # runs are already sorted (platform, branch, date)
        by_bp.setdefault((r["platform"], r["branch"]), []).append(r)
    for seq in by_bp.values():
        prev = None
        for r in seq:
            pol = r.get("policy")
            if pol:
                if prev:
                    changes = [{"field": f, "from": prev.get(f), "to": pol.get(f)}
                               for f in _POLICY_FIELDS if prev.get(f) != pol.get(f)]
                    if changes:
                        r["cfg_info"] = {"changes": changes}
                prev = pol


def _load_manual_annotations(root: Path) -> list[dict]:
    """Hand-written chart notes from ``results/annotations.yaml`` (optional file): a list of
    ``{date: ISO-date/time, text: str, platform: optional, branch: optional}``.  Rendered with
    the config-change markers; use for one-off events the data cannot self-describe (e.g. a
    merge whose BEFORE-side runs predate policy recording)."""
    p = root / "results" / "annotations.yaml"
    if not p.exists():
        return []
    try:
        items = _yaml_load(p.read_text()) or []
        return [i for i in items if isinstance(i, dict) and i.get("date") and i.get("text")]
    except Exception:
        return []


# ── Parse cache ───────────────────────────────────────────────────────────────
# The regression YAMLs are immutable once written, so their parsed run records are cached
# and only NEW files are parsed on each build (deletions fall out of the rescan).  The
# cache stores ONLY the _parse_run output (no gates/analysis), so tolerance and logic
# changes in this file never require invalidation.  ⚠ Bump _PARSE_CACHE_SCHEMA whenever
# the _parse_run/_parse_tests output shape changes.  The cache lives in the gitignored
# dashboard/ output dir; deleting it forces a full re-parse.
_PARSE_CACHE_SCHEMA = 2
_PARSE_CACHE_PATH = REPO_ROOT / "dashboard" / ".parse_cache.json"


def _file_sig(p: Path) -> list:
    try:
        st = p.stat()
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return [0, 0]


def _load_parse_cache() -> dict:
    try:
        cache = json.loads(_PARSE_CACHE_PATH.read_text())
        if cache.get("schema") == _PARSE_CACHE_SCHEMA:
            return cache.get("entries") or {}
    except (OSError, ValueError):
        pass
    return {}


def _save_parse_cache(entries: dict) -> None:
    # Written right after the scan loop, BEFORE the pipeline mutates the run records
    # (the correctness analyzer pops the private _fps/_pkgs fields in place).
    tmp = _PARSE_CACHE_PATH.with_suffix(".json.tmp")
    _PARSE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps({"schema": _PARSE_CACHE_SCHEMA, "entries": entries}))
    tmp.replace(_PARSE_CACHE_PATH)


def _parse_run(path: Path, platform: str, branch_dir: str) -> dict:
    """Parse one regression_<plat>_<date>.yaml into a compact run record."""
    doc = _yaml_load(path.read_text()) or {}
    date = str(doc.get("date") or "")
    branch = doc.get("git_branch") or branch_dir.replace("_", "/")
    gate = doc.get("gate") or {}
    cfg = doc.get("config") or {}
    tests = _parse_tests(path.parent / f"tests_{platform}_{date}.txt")
    # Fingerprint index for the correctness analyzer (cross-device / vs-main).  Private — stripped from
    # the run before the JSON is emitted (it would bloat window.__METRICS__; only findings are kept).
    fps = {}
    for c in (doc.get("cells") or []):
        fp = c.get("fingerprint")
        if fp:
            fps[f"{c.get('geometry')}|{c.get('op')}|{c.get('size')}|{c.get('n_devices')}"] = {m: fp.get(m) for m in _FP_FIELDS}
    return {
        "_fps": fps,
        # Full installed-package map ({name: version}), private like _fps: used to compute the dep-change
        # marker's per-package diff, then dropped from the emitted JSON (it would bloat window.__METRICS__).
        "_pkgs": doc.get("packages") or {},
        "platform": platform,
        "branch": branch,
        "branch_dir": branch_dir,
        "date": date,
        "commit_date": doc.get("git_commit_date"),  # ISO commit time (None on older runs)
        "measured_at": doc.get("measured_at"),       # ISO time this run was MEASURED (None on older runs)
        "commit": (doc.get("git_commit") or "")[:10],
        "commit_full": doc.get("git_commit") or "",
        # Dependency-canary provenance: dep_gen>0 marks a re-measure of a commit with a NEWER dep set (a
        # torch bump), so two runs can share a commit; run_reason names why; torch is the version measured.
        "dep_gen": int(doc.get("dep_gen") or 0),
        "run_reason": doc.get("run_reason") or "commit",
        "torch": (doc.get("toolchain") or {}).get("torch") if isinstance(doc.get("toolchain"), dict) else None,
        "torch_available": doc.get("torch_available"),   # PyPI-latest torch at run time (None when no
        #                             canary ran) — lets the marker say "available X, installed Y"

        "version": doc.get("mbirtorch_version"),
        "dirty": bool(doc.get("git_dirty", False)),
        "dirty_files": doc.get("git_dirty_files"),   # None on older runs
        "dirty_code": doc.get("git_dirty_code"),     # True = the mbirtorch/ package dir was dirty
        # Library policy defaults captured at measurement time (None on older runs) — the
        # config-change annotator diffs these between consecutive runs of a branch.
        "policy": doc.get("policy"),
        "device_label": doc.get("device_label"),
        "gate": {
            "result": gate.get("result"),
            "hard": _parse_gate_hard(gate.get("hard")),
            "soft": [str(s) for s in (gate.get("soft") or [])],
            "compared_to": gate.get("compared_to") or [],
        },
        # thresholds the gate uses, surfaced in the gate-tile explanation
        "gate_config": {k: cfg.get(k) for k in
                        ("mem_hard_pct", "speedup_warn_pct", "time_soft_pct",
                         "fp_rtol_single", "fp_rtol_iter")},
        "tests": tests,
        "cells": [_slim_cell(c) for c in (doc.get("cells") or [])],
    }


# --------------------------------------------------------------------------- #
# History y-ranges: the all-time [min, max] of each panel's PLOTTED value (the largest-size op cell's
# time in minutes / peak memory in GB) per geometry group, over ALL runs / branches / platforms /
# device counts.  The dashboard pins these as a fixed y-axis so the History panels don't rescale on a
# branch / geometry / n switch.  Mirrors dashboard.js `aggregate()`; ⚠ KEEP _HIST_GROUPS IN SYNC with
# HIST_GROUPS in dashboard.js.
# --------------------------------------------------------------------------- #
_HIST_GROUPS = [
    {"id": "cp", "geoms": ("cone", "parallel"), "op": "vcd_nonconst"},
    {"id": "tm", "geoms": ("translation", "multiaxis_parallel"), "op": "back"},
    {"id": "dn", "geoms": ("denoiser",), "op": "denoise"},
]


def _size_vol(size) -> int:
    try:
        return math.prod(int(x) for x in str(size).split("x"))
    except Exception:   # noqa: BLE001
        return 0


def _history_yranges(runs) -> dict:
    """{group_id: {"time": [min, max], "mem": [min, max]}} — see the section comment."""
    out = {}
    for g in _HIST_GROUPS:
        times, mems = [], []
        for r in runs:
            cells = r.get("cells") or []
            ndevs = {c.get("ndev") for c in cells if c.get("ndev") is not None}
            for gm in g["geoms"]:
                gm_cells = [c for c in cells if c.get("geom") == gm]
                if not gm_cells:
                    continue
                focus = max(_size_vol(c.get("size")) for c in gm_cells)   # largest measured size for this geom
                for nd in ndevs:
                    c = next((c for c in cells if c.get("geom") == gm and c.get("op") == g["op"]
                              and _size_vol(c.get("size")) == focus and c.get("ndev") == nd
                              and not c.get("failed")), None)
                    if not c:
                        continue
                    if c.get("min_ms") is not None:
                        times.append(c["min_ms"] / 60000.0)   # minutes (matches aggregate)
                    if c.get("mem_mb") is not None:
                        mems.append(c["mem_mb"] / 1024.0)      # GB (matches aggregate)
        rng = {}
        if times:
            rng["time"] = [min(times), max(times)]
        if mems:
            rng["mem"] = [min(mems), max(mems)]
        if rng:
            out[g["id"]] = rng
    return out


def _perop_yranges(runs) -> dict:
    """{"{platform}|{geom}|{op}": {"time": [min, max], "mem": [min, max]}} — a fixed y-axis for the
    per-op History panels, spanning ALL branches, sinogram shapes, device counts and runs of that
    (platform, geom, op).  Keyed by (platform, geom, op) — NOT (platform, op) — because a shared op like
    `back` runs across geometries whose problem sizes differ by decades, so a cross-geometry range would
    be useless; within one geometry it stays ~the shape-sweep span.  Independent of the selected
    shape/branch/n so switching them doesn't rescale (see dashboard.js renderPerOp)."""
    acc: dict[str, dict] = {}
    for r in runs:
        plat = r.get("platform")
        for c in (r.get("cells") or []):
            if c.get("failed"):
                continue
            geom, op = c.get("geom"), c.get("op")
            if geom is None or op is None:
                continue
            a = acc.setdefault(f"{plat}|{geom}|{op}", {"time": [], "mem": []})
            if c.get("min_ms") is not None:
                a["time"].append(c["min_ms"] / 60000.0)   # minutes
            if c.get("mem_mb") is not None:
                a["mem"].append(c["mem_mb"] / 1024.0)      # GB
    out = {}
    for key, a in acc.items():
        rng = {}
        if a["time"]:
            rng["time"] = [min(a["time"]), max(a["time"])]
        if a["mem"]:
            rng["mem"] = [min(a["mem"]), max(a["mem"])]
        if rng:
            out[key] = rng
    return out


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #
def collect_data() -> dict:
    results_dir = REPO_ROOT / "results"

    runs: list[dict] = []
    records: dict[str, dict] = {}
    platforms: set[str] = set()
    branches: set[str] = set()

    parse_cache = _load_parse_cache()
    new_parse_cache: dict = {}
    for plat_dir in sorted(p for p in results_dir.glob("*") if p.is_dir()):
        platform = plat_dir.name
        if ONLY_PLATFORMS and platform not in ONLY_PLATFORMS:
            continue
        for branch_dir_path in sorted(p for p in plat_dir.glob("*") if p.is_dir()):
            branch_dir = branch_dir_path.name
            # NB: exclude the sibling *_table.yaml (the browsable per-run dump written next to each run)
            # — it matches this glob but is NOT a run record, and parsing it as one yields a phantom
            # branch (no top-level git_branch -> dir-slug fallback) and a garbage date.
            run_files = sorted(f for f in branch_dir_path.glob(f"regression_{platform}_*.yaml")
                               if not f.name.endswith("_table.yaml"))
            if not run_files:
                continue
            for rf in run_files:
                key = str(rf.relative_to(REPO_ROOT))
                sig = _file_sig(rf)
                hit = parse_cache.get(key)
                # A hit must match the yaml signature AND its companion tests file's
                # signature (the tests path is only known after a parse, so it is
                # remembered in the entry; yaml immutability makes this sound).
                if hit and hit["sig"] == sig and _file_sig(Path(REPO_ROOT, hit["tests"])) == hit["tests_sig"]:
                    run = hit["run"]
                else:
                    run = _parse_run(rf, platform, branch_dir)
                    tests_rel = str((rf.parent / f"tests_{platform}_{run['date']}.txt").relative_to(REPO_ROOT))
                    hit = {"sig": sig, "tests": tests_rel,
                           "tests_sig": _file_sig(Path(REPO_ROOT, tests_rel)), "run": run}
                new_parse_cache[key] = hit
                if ONLY_BRANCHES and run["branch"] not in ONLY_BRANCHES:
                    continue
                runs.append(run)
                platforms.add(platform)
                branches.add(run["branch"])
            # Records book (best-ever) — one per platform+branch.
            rec_path = branch_dir_path / f"records_{platform}.yaml"
            if rec_path.exists():
                branch_name = runs[-1]["branch"] if runs else branch_dir.replace("_", "/")
                records[f"{platform}|{branch_name}"] = _yaml_load(rec_path.read_text()) or {}

    _save_parse_cache(new_parse_cache)   # before the pipeline mutates the records in place
    runs.sort(key=lambda r: (r["platform"], r["branch"], r["date"]))
    # Correctness analyzer (P2/P4): annotate each run with cross-device + vs-main + cross-platform + prior
    # findings (+ the cross-device noise floor), then drop the private fingerprint index from the JSON.
    corr_stats = _analyze_correctness(runs)
    # Dependency-canary: annotate each re-measure with what its dep set changed (marker tooltip), then drop
    # the private fingerprint + package maps from the JSON (findings / dep_info are what's kept).
    _annotate_dep_info(runs)
    _annotate_config_changes(runs)
    for r in runs:
        r.pop("_fps", None)
        r.pop("_pkgs", None)
    # Correctness "reviewed-through" watermark (design note D6): a single committed date; any correctness
    # divergence on a commit dated <= this is treated as acknowledged (greyed, dropped from the banner /
    # tab badge).  Absent file => nothing acknowledged.  The guided clear script (P3) writes this field.
    cleared_through = _read_cleared_through(REPO_ROOT)
    return {
        "annotations": _load_manual_annotations(REPO_ROOT),
        "generated": _generated_stamp(),
        "repo_name": REPO_ROOT.name,
        "repo_url": _repo_url(),
        "platforms": sorted(platforms),
        "branches": sorted(branches),
        "runs": runs,
        "records": records,
        "cleared_through": str(cleared_through) if cleared_through else None,
        "corr_tol": {"single": VSMAIN_RTOL_SINGLE, "iter": VSMAIN_RTOL_ITER, "xdev": XDEV_RTOL, "xplat": VSPLAT_RTOL_SINGLE},
        "corr_stats": corr_stats,
        "hist_yranges": _history_yranges(runs),   # fixed y-axis per (geom-group, metric) for the History panels
        "perop_yranges": _perop_yranges(runs),     # fixed y-axis per (platform, geom, op) for the per-op panels
    }


# --------------------------------------------------------------------------- #
# Assembly                                                                    #
# --------------------------------------------------------------------------- #
def _correctness_summary(data: dict) -> None:
    """Print the CORRECTNESS ALERT block + the cross-device noise floor (design note D5/P5).  Emitted at
    the end of every dashboard build, so the nightly's rebuild surfaces unacknowledged correctness
    divergences (the same set as the dashboard banner) and the floor is there to tune the tolerances."""
    runs = data.get("runs") or []
    through = data.get("cleared_through")

    def _t(r):
        return r.get("commit_date") or r.get("date") or ""

    def _rdate(r):
        cd = r.get("commit_date")
        if cd:
            return cd[:10]
        d = r.get("date") or ""
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if (len(d) == 8 and d.isdigit()) else None

    latest: dict = {}
    for r in runs:
        k = (r["platform"], r["branch"])
        if k not in latest or _t(r) > _t(latest[k]):
            latest[k] = r

    def _acked(r):
        d = _rdate(r)
        return through is not None and d is not None and d <= through

    bad = sorted((r for r in latest.values() if r.get("correctness") and not _acked(r)), key=_t, reverse=True)
    print("\n" + "=" * 78)
    if bad:
        print(f"  CORRECTNESS ALERT — {len(bad)} branch run(s) diverge (unacknowledged):")
        print("=" * 78)
        for r in bad:
            print(f"\n  {r['platform']}/{r['branch']} @ {r['commit']} ({_rdate(r)})")
            for c in sorted({f["cell"] for f in r["correctness"]}):
                print(f"      {_fmt_cell(c)}")
        print("\n  Review, then acknowledge with action_scripts/clear_correctness.sh")
    else:
        tail = f"  (cleared through {through})" if through else ""
        print(f"  CORRECTNESS: no unacknowledged divergences.{tail}")
        print("=" * 78)
    st = data.get("corr_stats") or {}
    if st.get("xdev_floor") is not None:
        print(f"  cross-device noise floor: {st['xdev_floor']:.2e} over {st['xdev_n']} comparison(s) "
              f"— tune the cross-* tolerances against this.")
    if st.get("xplat_floor") is not None:
        print(f"  cross-platform (CPU<->GPU) floor: {st['xplat_floor']:.2e} over {st['xplat_n']} shared cell(s) "
              f"— calibrates VSPLAT_RTOL_* (now {VSPLAT_RTOL_SINGLE:g}/{VSPLAT_RTOL_ITER:g}).")


def build() -> Path:
    data = collect_data()

    template = (HERE / "template.html").read_text()
    css = (HERE / "dashboard.css").read_text()
    js = (HERE / "dashboard.js").read_text()
    uplot_js = (HERE / "vendor" / "uPlot.iife.min.js").read_text()
    uplot_css = (HERE / "vendor" / "uPlot.min.css").read_text()

    # json.dumps is safe to inline as long as we neutralise the only sequence that
    # can prematurely close a <script> element.
    data_json = json.dumps(data, allow_nan=False, default=str).replace("</", "<\\/")

    html = (
        template
        .replace("/*{{UPLOT_CSS}}*/", uplot_css)
        .replace("/*{{DASHBOARD_CSS}}*/", css)
        .replace("/*{{UPLOT_JS}}*/", uplot_js)
        .replace("/*{{DATA}}*/", "window.__METRICS__ = " + data_json + ";")
        .replace("/*{{DASHBOARD_JS}}*/", js)
    )

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(html)

    n_cells = sum(len(r["cells"]) for r in data["runs"])
    print(f"Discovered {len(data['runs'])} run(s), {n_cells} config(s) "
          f"across platforms={data['platforms']} branches={data['branches']}.")
    print(f"Wrote {OUT_PATH}  ({OUT_PATH.stat().st_size / 1024:.0f} KB)")
    print(f"Open it with:  open '{OUT_PATH}'")
    _correctness_summary(data)
    return OUT_PATH


if __name__ == "__main__":
    build()
