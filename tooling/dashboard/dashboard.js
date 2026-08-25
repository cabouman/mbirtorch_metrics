"use strict";
// Client logic for the mbirtorch metrics dashboard.  Data is pre-parsed by
// build_dashboard.py and embedded as window.__METRICS__; this script reads it
// and renders.  Charts are uPlot (vendored, inlined); no network, no framework.

const M = window.__METRICS__;
const $ = (id) => document.getElementById(id);

// ---- colours (canvas can't read CSS vars, so these are fixed; chosen to read
// in both light and dark mode) -------------------------------------------------
const DEVC = { 1: "#378ADD", 2: "#1D9E75", 4: "#D85A30" };           // by device count
const SIZEC = ["#378ADD", "#1D9E75", "#D85A30", "#7F77DD", "#BA7517"]; // by size index
const GEOMC = { cone: "#D85A30", parallel: "#378ADD" };               // by geometry
const PLATC = { gpu: "#185fa5", cpu: "#BA7517" };                     // history: by platform
// This repository records one backend, so a platform key is just the hardware it ran on and the
// display label is the key itself.
const platLabel = (p) => p;
const platLabels = () => M.platforms.map(platLabel);   // parallel to M.platforms, for fillSelect
const IDEAL = "#9b9b94", FAILC = "#E24B4A", REFC = "#1f1f1d"; // ideal=grey, gate=red, reference=near-black
const THROTC = "#E8950C";                                    // amber: a GPU ran hot / throttled (timing unreliable)
const CORRC = "#a32d2d";                                      // deep red: a CORRECTNESS divergence (more severe than a perf hit)
const DEPC = "#25a244";
const CFGC = "#a855f7";   // purple: a CONFIG/default change (library policy defaults) or a hand-written note
// DEPC above is green: a DEPENDENCY change (torch bump / deps refresh).  Both are ruler changes
// rather than series, and both are kept clear of the blue and amber series colours.
const HOT_C = 85, HOT_HBM = 95;                              // a cell is "hot" if a GPU core>=85C / HBM>=95C / any throttle reason
const BRANCH_DASH = [null, [5, 3], [2, 2], [6, 2, 2, 2]];
const devColor = (n) => DEVC[n] || SIZEC[n % SIZEC.length];
// Per-op history colours a curve by BRANCH (platform + op are fixed by that section's selectors, so the
// platform colour can't disambiguate).  A stable palette indexed by the branch's position in M.branches.
const BRANCHC = ["#185fa5", "#1D9E75", "#D85A30", "#7F77DD", "#BA7517", "#C0468A", "#3AA6A6", "#8A8A2E"];
const branchColor = (b) => BRANCHC[Math.max(0, M.branches.indexOf(b)) % BRANCHC.length];

const OP_ORDER = ["direct_filter", "forward", "back", "vcd_nonconst", "denoise"];
const GEOM_ORDER = ["parallel", "cone", "translation", "multiaxis_parallel", "denoiser"];
// History line-style by geometry (one solid + one dashed within each group); short legend labels.
const GEOM_DASH = { cone: undefined, parallel: [5, 3], translation: undefined, multiaxis_parallel: [5, 3], denoiser: undefined };
const GEOM_LABEL = { cone: "cone", parallel: "parallel", translation: "translation", multiaxis_parallel: "multiaxis", denoiser: "denoiser" };
// History geometry GROUPS: a toggle swaps the set shown.  cone/parallel headline the vcd recon;
// translation/multiaxis don't run vcd (only projectors + filter), so they headline back-projection;
// the denoiser is its own group (a single geometry) headlining its one op, denoise (the vcd analog).
const HIST_GROUPS = [
  { id: "cp", label: "cone + parallel", geoms: ["cone", "parallel"], op: "vcd_nonconst", opLabel: "VCD" },
  { id: "tm", label: "translation + multiaxis", geoms: ["translation", "multiaxis_parallel"], op: "back", opLabel: "back-projection" },
  { id: "dn", label: "denoiser", geoms: ["denoiser"], op: "denoise", opLabel: "denoise" },
];

// Expected (ideal) time-scaling per op, for the roughly cubical sweep shapes.
// The x-axis is sinogram entries (∝ N³ for cubic), so cost ∝ N^k maps to x^(k/3):
//   filter ∝ sinogram entries (N³) → x¹ ; forward/back ∝ voxels (N³) → x¹ ;
//   vcd ∝ voxels·views (N⁴) → x^(4/3).
const IDEAL_EXP = { direct_filter: 1, forward: 1, back: 1, vcd_nonconst: 4 / 3 };
const IDEAL_BASIS = { direct_filter: "sinogram entries", forward: "voxels", back: "voxels", vcd_nonconst: "voxels · views" };

const ui_state = { platform: null, branch: null, go: null, ref: "none", view: "plot", openTile: null, runKey: null, histN: 1, histGroup: "cp", histBranch: "all",
  poPlat: null, poBranch: "all", poGo: null, poShape: null, poN: 1 };   // per-op history: platform, branch, geom|op, sino shape, device count

// Displayed name for each reference (internal key -> label).  References are now derived from the
// tracked runs themselves (latest main/prerelease tip, this branch's prior run) + best-ever.
const REF_LABEL = { main: "main", prerelease: "prerelease", prior: "prior run", best: "best-ever" };

// ---- generic helpers ---------------------------------------------------------
const uniq = (a) => [...new Set(a)];
const cellKey = (c) => `${c.geom}|${c.op}|${c.size}|${c.ndev}`;
// "geom|op|size|ndev" -> "geom, op, size, n_devices=N" (the human config label, shared by the banner
// and the correctness/perf drill-downs).
const cellCoords = (k) => { const p = (k || "").split("|"); return p.length === 4 ? `${p[0]}, ${p[1]}, ${p[2]}, n_devices=${p[3]}` : (k || ""); };
const sizeVol = (s) => s.split("x").reduce((p, n) => p * (+n || 1), 1);
// Sort by COMMIT time (not collection date), so "latest" = the most recent COMMIT.  Otherwise an
// add_run of an OLD commit (collected recently) would sort last and become the default run shown.
const runsFor = (p, b) => M.runs.filter((r) => r.platform === p && r.branch === b).sort((a, b2) => runTime(a) - runTime(b2));
const latestRun = (p, b) => { const r = runsFor(p, b); return r.length ? r[r.length - 1] : null; };
// The run currently being viewed: the one the user picked (ui_state.runKey), else latest.
function currentRun() {
  const rs = runsFor(ui_state.platform, ui_state.branch);
  if (!rs.length) return null;
  if (ui_state.runKey) { const m = rs.find((r) => runKey(r) === ui_state.runKey); if (m) return m; }
  return rs[rs.length - 1];
}
// A run's position in time: the commit's date when recorded, else the collection
// date.  Lets older prerelease checkouts sit at their real point on the timeline.
// x-position on the history timeline.  Normally the commit time — but a dependency-canary re-run
// (dep_gen>0) shares its commit's time, so plot it at when it was MEASURED (measured_at, else its
// collection date), otherwise it would stack on / overwrite the commit's original point (aggByPB and
// the tooltip both key runs by runTime).
const runTime = (r) => (r.dep_gen ? (r.measured_at ? Date.parse(r.measured_at) / 1000 : dateToUnix(r.date))
                                  : (r.commit_date ? Date.parse(r.commit_date) / 1000 : dateToUnix(r.date)));
// Unique handle for a picked run.  The collection `date` (YYYYMMDD) is NOT unique — two commits
// measured the same day share it — so a click resolved by date alone returned the earlier-committed
// run even though the tooltip (which keys on commit time) named the one clicked.  Key on the commit
// too: commit-sha # collection-date # commit-time disambiguates same-day commits and re-measures.
const runKey = (r) => (r.commit_full || r.commit || "?") + "#" + (r.date || "") + "#" + (r.commit_date || "") + "#g" + (r.dep_gen || 0);
const runDateLabel = (r) => (r.commit_date ? r.commit_date.slice(0, 10) : dateLabel(r.date));
// Commit date AND time, to the minute (e.g. "2026-06-17 23:00") — the unambiguous run stamp.
// ISO is "YYYY-MM-DDTHH:MM:SS±zz"; slice to the minute and swap the T for a space.
const commitMinute = (r) => (r.commit_date ? r.commit_date.slice(0, 16).replace("T", " ") : (r.date ? dateLabel(r.date) : "?"));
// ---- correctness (severity split, design note D1/D6) -------------------------------------------
// The "reviewed-through" watermark (a single date from results/correctness_acks.yaml): a run whose
// commit is dated <= this is acknowledged — kept visible but greyed and dropped from the banner/badge.
// Compare by the commit's LOCAL calendar date (runDateLabel -> "YYYY-MM-DD"), NOT a UTC timestamp, so it
// matches clear_correctness.py / the build analyzer (both use commit_date[:10]).  An evening commit in a
// negative-offset zone is "the 22nd" locally even though it's already the 23rd in UTC — a UTC compare
// would leave it un-acknowledged and keep the banner up after clear_correctness said "nothing to clear".
// Correctness findings are computed corpus-wide by build_dashboard (prior + cross-device + vs-main),
// each {reference, cell, basis, discrepancies[]}.  Perf hard hits stay on gate.hard (kind != correctness).
const runCorr = (r) => (r.correctness || []);
const runCorrCells = (r) => new Set(runCorr(r).map((f) => f.cell)).size;   // distinct divergent configs
const runPerfHard = (r) => (r.gate && r.gate.hard || []).filter((h) => h.kind !== "correctness");
const runIncorrect = (r) => runCorr(r).length > 0;            // diverges on at least one cell/reference
const runAcked = (r) => M.cleared_through != null && runDateLabel(r) <= M.cleared_through;
const runAlert = (r) => runIncorrect(r) && !runAcked(r);     // unacknowledged -> drives banner/badge/markers
// A gate basis / compared_to string is the prior RUN's filename ("prior:regression_<plat>_<ts>_<sha>.yaml").
// Render it like a run entry — "GPU · <commit> · <commit date+time>" — by resolving its sha to that run.
function priorLabel(fn, plat) {
  const m = /_([0-9a-f]{7,40})\.ya?ml$/.exec(fn || "");
  const sha = m ? m[1] : null;
  if (!sha) return fn || "?";
  const r = M.runs.find((x) => x.platform === plat && x.commit_full && x.commit_full.startsWith(sha));
  return r ? `${plat.toUpperCase()} · ${r.commit} · ${commitMinute(r)}` : `${plat.toUpperCase()} · ${sha.slice(0, 10)}`;
}
// Compact form of a gate basis for a tooltip: "prior:regression_gpu_..._0fe76ae2.yaml" -> "prior 0fe76ae2"
// (the comparison TYPE + the reference sha), so "expected" says WHAT it was compared against.
function gateBasisShort(basis) {
  if (!basis) return null;
  const type = (String(basis).split(":")[0] || "").trim();
  const m = /_([0-9a-f]{7,40})\.ya?ml$/.exec(basis);
  const sha = m ? m[1].slice(0, 8) : null;
  return sha ? (type ? `${type} ${sha}` : sha) : (type || String(basis));
}
// The "⚠ gate fail — <discrepancy>" line for a scaling tooltip.  Memory discrepancies arrive from the
// engine in MB; show them in GB (via fmtGB) to match the rest of the tooltip — time details carry no
// "MB", so that conversion is a no-op there.  The reference is then folded into the engine's
// "... expected ..." phrasing (falls back to appending "· vs <ref>" if that wording ever changes).
function gateNote(detail, basis) {
  const bl = gateBasisShort(basis);
  let d = (detail || "").replace(/([\d.]+)\s*MB/g, (_, n) => fmtGB(parseFloat(n)));
  d = !bl ? d : (d.includes(" expected") ? d.replace(" expected", " expected from " + bl) : (d + " · vs " + bl));
  return `<span class="bad">⚠ gate fail</span> — ${d}`;
}
// The run shown for a given platform on the currently-selected branch: honour an explicitly
// picked run (ui_state.runKey) only on the active platform; otherwise the latest for that platform.
function runOnPlat(plat) {
  const rs = runsFor(plat, ui_state.branch);
  if (!rs.length) return null;
  // Anchor BOTH platforms to the shown run's COMMIT, so every split tile (and the per-platform drill-down)
  // reflects exactly that commit.  A platform that did not measure this commit returns null here -> shows
  // '—'; we do NOT substitute its latest run (that would mix commits).  Only an absent anchor commit (old
  // data with no commit_full) falls back to latest, so the page still renders.
  const cur = currentRun();
  if (!cur || !cur.commit_full) return rs[rs.length - 1];
  return rs.filter((r) => r.commit_full === cur.commit_full).sort((a, b) => runTime(b) - runTime(a))[0] || null;
}
// The run backing the active reference overlay: the tracked main/prerelease tip, or this branch's
// immediately-preceding run.  best-ever is records-derived (not a single run) -> handled separately.
function refRun() {
  if (ui_state.ref === "main") return latestRun(ui_state.platform, "main");
  if (ui_state.ref === "prerelease") return latestRun(ui_state.platform, "prerelease");
  if (ui_state.ref === "prior") {
    const rs = runsFor(ui_state.platform, ui_state.branch), cur = currentRun();
    const i = cur ? rs.indexOf(cur) : -1;
    return i > 0 ? rs[i - 1] : null;
  }
  return null;
}
// Branch/sha/date provenance string for the active comparison reference.
function refProvenance() {
  if (ui_state.ref === "best") return "per-config best-ever";
  const r = refRun(); if (!r) return "";
  const d = r.commit_date ? r.commit_date.slice(0, 10) : (r.date ? runDateLabel(r) : "");
  return `${r.branch || "?"}${r.commit ? " @ " + r.commit : ""}${d ? " · " + d : ""}`;
}
const branchesFor = (p) => uniq(M.runs.filter((r) => r.platform === p).map((r) => r.branch)).sort();
const findCell = (run, key) => run.cells.find((c) => cellKey(c) === key) || null;
// CAUSAL signal: the driver actually clamped clocks (a clocks_throttle_reasons.* fired during the
// cell).  This is what degrades a measured time — distinct from merely running hot.  (Only the new
// engine records throttle reasons, so this stays empty on runs measured before that landed.)
function cellThrottled(c) {
  return !!(c && c.gpu && c.gpu.some((g) => g.thr && g.thr.length));
}
// A cell worth flagging because a GPU ran hot OR throttled during it.  Re-derived client-side from
// the per-GPU temps (c.gpu, present only for flagged cells) so OLD runs measured before the throttle
// detector was tightened still light up — plus the engine's own `throttled` flag.  Superset of
// cellThrottled: "hot" (temperature) is advisory; "throttled" (clocks clamped) is causal.
function cellHot(c) {
  if (!c) return false;
  if (c.throttled || cellThrottled(c)) return true;
  return !!(c.gpu && c.gpu.some((g) => (g.t || 0) >= HOT_C || (g.mt || 0) >= HOT_HBM));
}
// "which GPU, how hot, why" for the tooltip (the hottest GPU on the cell).
function hotGpuStr(c) {
  const gs = c && c.gpu;
  if (!gs || !gs.length) return c && c.throttled ? "throttled" : "";
  const w = gs.reduce((a, b) => ((b.t || 0) > (a.t || 0) ? b : a), gs[0]);
  let s = `GPU${w.i} ${w.t}°C`;
  if (w.mt != null) s += ` · HBM ${w.mt}°C`;
  if (w.sm != null) s += ` · sm ${w.sm}` + (w.mem != null ? `/mem ${w.mem}` : "") + " MHz";
  if (w.thr) s += ` · ${w.thr.join(", ")}`;
  return s;
}
// Tooltip warning text: lead with the ui_state word so "hot" (advisory) reads distinctly from
// "throttled" (the driver clamped clocks, so the time is suspect).
function hotWarn(c) { return (cellThrottled(c) ? "throttled" : "hot") + " · " + hotGpuStr(c); }
// Run-level thermal summary for the "run shown" tile: the worst severity present (throttled beats
// hot), the device counts (n) it hit, and the peak core temp — null when the run was thermally fine.
function runThermal(run) {
  const acc = { throttled: { devs: new Set(), t: 0 }, hot: { devs: new Set(), t: 0 } };
  (run.cells || []).forEach((c) => {
    const sev = cellThrottled(c) ? "throttled" : (cellHot(c) ? "hot" : null);
    if (!sev) return;
    if (c.ndev != null) acc[sev].devs.add(c.ndev);
    const pk = Math.max(0, ...((c.gpu || []).map((g) => g.t || 0)));
    if (pk > acc[sev].t) acc[sev].t = pk;
  });
  const sev = acc.throttled.devs.size ? "throttled" : (acc.hot.devs.size ? "hot" : null);
  return sev ? { sev, ndevs: [...acc[sev].devs].sort((a, b) => a - b), peak: acc[sev].t } : null;
}
const dateToUnix = (d) => Date.UTC(+d.slice(0, 4), +d.slice(4, 6) - 1, +d.slice(6, 8)) / 1000;
const dateLabel = (d) => `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`;

function fillSelect(id, values, current, labels) {
  $(id).innerHTML = values.map((v, i) =>
    `<option value="${v}" ${v == current ? "selected" : ""}>${labels ? labels[i] : v}</option>`).join("");
}
function fmtGB(mb) { return mb == null ? "—" : (mb / 1024).toFixed(mb / 1024 < 10 ? 2 : 1) + " GB"; }
function fmtNum(v) { if (v == null) return ""; if (v >= 100) return v.toFixed(0); if (v >= 1) return v.toFixed(1); if (v > 0) return v.toFixed(2); return "0"; }
// Tooltip time: input is in MINUTES; show seconds when under a minute (a sub-minute run reads better as
// "0.80 s" than "0.01 min"), minutes at or above one.  '—' for null.
function fmtTime(min) { return min == null ? "—" : (min < 1 ? (min * 60).toFixed(2) + " s" : min.toFixed(2) + " min"); }
// Log axes, WIDE range (a decade or more): label only exact powers of ten and blank the minor
// ticks, because uPlot would otherwise label every minor gridline and that reads as a stack of
// noise.  A NARROWER range is labelled differently; see logAxisValues.
function logFmt(v) {
  if (v == null) return "";
  const l = Math.log10(v);
  if (Math.abs(l - Math.round(l)) > 1e-6) return "";
  return v >= 1 ? String(v) : v.toString();
}
// The decimals a tick step needs to print exactly: 0.005 -> 3, 2.5 -> 1, 5 -> 0.
function stepDecimals(step) {
  for (let d = 0; d <= 6; d++) {
    const scaled = step * Math.pow(10, d);
    if (Math.abs(scaled - Math.round(scaled)) < 1e-9) return d;
  }
  return 6;
}
// A log window is NARROW when the powers-of-ten rule would put fewer than two numbers on it: it
// holds at most one exact power of ten.  Such a window gets tick marks it cannot label, which is
// what makes it unreadable, so logTicks and logAxisValues both serve it differently.  The test is
// the COUNT of powers of ten in the window rather than its ratio, because the ratio gets the
// boundary wrong: the scaling panel's [0.076, 0.80] spans a ratio of 10.5 and still holds only
// 0.1.
function narrowLogWindow(mn, mx) {
  if (!(mn > 0) || !(mx > 0) || !isFinite(mn) || !isFinite(mx) || mx <= mn) return false;
  // Counted the way logTicks emits them, with the same comparison, so the predicate cannot
  // disagree with what is drawn.  Comparing the logs instead misses by an ulp: a window whose top
  // is the float just below 1e-5 reads as a full decade and is served as one, while the tick
  // generator leaves 1e-5 out and the axis ends up with a single label.
  let count = 0;
  for (let e = Math.floor(Math.log10(mn)); e <= Math.ceil(Math.log10(mx)); e++) {
    const v = Math.pow(10, e);
    if (v >= mn && v <= mx) count++;
  }
  return count < 2;
}
// Log-axis tick labels.  On a wide range this is logFmt, unchanged.  On a narrow one logTicks has
// switched to an evenly spaced grid and every tick on it is labelled.  The decimals come from the
// grid's own step, so the labels read as one column.
function logAxisValues(scale, splits) {
  const narrow = scale && narrowLogWindow(scale.min, scale.max);
  if (!narrow) return splits.map(logFmt);
  const step = splits.length > 1 ? Math.abs(splits[1] - splits[0]) : 0;
  const dec = stepDecimals(step > 0 ? step : (splits[0] || 1));
  return splits.map((v) => (v == null ? "" : v.toFixed(dec)));
}
// Evenly spaced ticks across [mn, mx]: the SMALLEST step from the 1, 2, 2.5, 5 x 10^k ladder whose
// tick count stays at or under NARROW_MAX_TICKS.  Ascending that ladder is what keeps the count
// near the cap rather than far below it; dividing the span by a target count and rounding to a
// nice number undershoots badly on some spans (0.0285 / 5 rounds up to a step of 0.01, which puts
// two ticks on the axis, where this puts five).  The cap is 9 so that a window covering most of
// one decade keeps the 1-9 x 10^k gridlines it already had and merely gains their labels.
const NARROW_MAX_TICKS = 9;
function evenTicks(mn, mx) {
  const span = mx - mn;
  if (!(span > 0) || !isFinite(span)) return [mn, mx];
  const lo = Math.floor(Math.log10(span)) - 3, hi = Math.ceil(Math.log10(span)) + 1;
  for (let e = lo; e <= hi; e++) {
    for (const m of [1, 2, 2.5, 5]) {
      const step = m * Math.pow(10, e);
      const first = Math.ceil(mn / step - 1e-9);
      const count = Math.floor(mx / step + 1e-9) - first + 1;
      if (count > NARROW_MAX_TICKS) continue;
      if (count < 1) return [mn, mx];
      const dec = stepDecimals(step), out = [];
      for (let k = first; out.length < count; k++) out.push(+((k * step).toFixed(dec + 2)));
      return out;
    }
  }
  return [mn, mx];
}
// Log-axis tick positions (1-9·10^k within [mn, mx]) that we hand to uPlot explicitly instead of
// letting its built-in log splitter generate them.  On a TIGHT, log-padded scale whose bounds aren't
// round powers of ten (e.g. a y-min of ~9.8e-5 left by the 6% pad), that splitter degrades into a
// near-stationary increment and spends seconds building a giant tick array — it froze the GPU
// parallel/back time panel for ~2.5s on op-switch (the new 513³ run's timings happened to land the
// padded y-min just below 1e-4).  This generator is O(decades) and bounded.  logFmt still labels only
// the exact powers of ten, so the look is unchanged (minor 2-9·10^k stay as faint unlabeled grid).
function logTicks(mn, mx) {
  if (!(mn > 0) || !(mx > 0) || !isFinite(mn) || !isFinite(mx)) return [mn, mx];
  // A NARROW window (see narrowLogWindow) cannot be served by the powers-of-ten labels, so it gets
  // an evenly spaced grid whose every tick is labelled.  The denoiser History time panel is the
  // case that found this: its 42 runs span 0.0316 to 0.0537 minutes, the padded scale is
  // [0.0304, 0.0589], and the old grid offered exactly 0.04 and 0.05, neither of them labelled.
  // Inside one decade a log axis is close to linear, so an even grid sits where the eye expects it.
  if (narrowLogWindow(mn, mx)) return evenTicks(mn, mx);
  const out = [], hi = Math.ceil(Math.log10(mx));
  for (let e = Math.floor(Math.log10(mn)); e <= hi; e++) {
    const base = Math.pow(10, e);
    for (let m = 1; m < 10; m++) { const v = m * base; if (v >= mn && v <= mx) out.push(v); }
  }
  return out.length ? out : [mn, mx];
}

// ---- uPlot wrapper -----------------------------------------------------------
// specs: [{label, color, ys, dash, pointsOnly, width, psize}]
// Fixed LOG y-axis for the History panels: PROPORTIONAL margins (a fixed % of plot height top & bottom,
// in log space) instead of a floor-to-decade bottom + fixed-multiple top, plus a fixed marker height in
// the top (and, when bottomBand, the bottom) margin.  Given the data extent [dmin, dmax], solve span so
// dmin sits FB of the height up from bot and dmax sits FT down from top; park off-scale marks at MK of the
// top margin above dmax (so failed/OOM/dep marks are separated at a CONSISTENT height regardless of the
// view's data).  bottomBand enlarges the bottom margin to FB_MARK and returns markBot at MK of it below
// dmin, giving the dep marker's second (bottom) glyph the same headroom.  null if non-positive.
// The seconds unit's axis label, defined once so the time panels cannot spell it differently.
const SECONDS_LABEL = "sec";
const HIST_Y_FB = 0.06, HIST_Y_FB_MARK = 0.14, HIST_Y_FT = 0.14, HIST_Y_MK = 0.5;
function fixedLogYRange(dmin, dmax, bottomBand) {
  if (!(dmin > 0) || !(dmax > 0)) return null;
  let lo = Math.log10(dmin), hi = Math.log10(dmax);
  if (hi - lo < 0.05) { const c = (lo + hi) / 2; lo = c - 0.15; hi = c + 0.15; }   // degenerate extent -> small window
  const fb = bottomBand ? HIST_Y_FB_MARK : HIST_Y_FB;   // enlarge the bottom margin only when it holds a marker
  const span = (hi - lo) / (1 - fb - HIST_Y_FT);
  return {
    bot: Math.pow(10, lo - fb * span),
    top: Math.pow(10, hi + HIST_Y_FT * span),
    mark: Math.pow(10, hi + HIST_Y_MK * HIST_Y_FT * span),
    markBot: bottomBand ? Math.pow(10, lo - HIST_Y_MK * fb * span) : null,
  };
}
function linePlot(el, xs, specs, o) {
  o = o || {};
  const cs = getComputedStyle(document.body);
  const axc = (cs.getPropertyValue("--muted").trim() || "#888");
  const grc = (cs.getPropertyValue("--border").trim() || "#ddd");
  const bg = (cs.getPropertyValue("--bg").trim() || "#fff");
  // Optional data-domain padding: extend the x-domain on each end with a null-y column, so the
  // extreme points/ticks aren't jammed against the panel edge — WITHOUT pinning a fixed scale range,
  // which would hard-clamp the scale and block drag-zoom (#6).  Auto-range keeps zoom working (the
  // prepended pad column shifts the data index by padOff, undone for the tooltip/click callbacks).
  //   o.xPad    — multiplicative factor (log axes, e.g. the scaling size axis)
  //   o.xPadAdd — additive amount in x-units (linear axes, e.g. 5% of the span on the time-history
  //               axis, so the first/last run isn't on the boundary where a click misses — the
  //               tooltip's 30px grab still finds it, but u.cursor.idx comes back null right at the edge).
  const padOff = ((o.xPad || o.xPadAdd) && xs.length > 1) ? 1 : 0;
  const xLo = o.xPadAdd ? xs[0] - o.xPadAdd : xs[0] / o.xPad;
  const xHi = o.xPadAdd ? xs[xs.length - 1] + o.xPadAdd : xs[xs.length - 1] * o.xPad;
  const X = padOff ? [xLo, ...xs, xHi] : xs;
  const S = padOff ? specs.map((s) => ({ ...s, ys: [null, ...s.ys, null] })) : specs;
  const data = [X, ...S.map((s) => s.ys)];
  // Off-scale markers (dep changes + failed/OOM configs) float ABOVE the data at markY = depTopMul × the
  // tallest data point (default 2×); the y-scale is stretched to keep that headroom (see yScale.range).
  // Null when there are no such marks or no finite data — the draw hooks then fall back to a fixed band.
  // Fixed y-axis (o.yRange + o.yLog): a LOG range with proportional top/bottom margins (fixedLogYRange).
  const yFixR = (o.yLog && o.yRange && o.yRange[0] != null && o.yRange[1] != null)
    ? fixedLogYRange(o.yRange[0], o.yRange[1], !!(o.depMarks && o.depMarks.length)) : null;
  // Box-zoom on a fixed-range panel (o.boxZoom): a y-drag selection overrides the pinned [bot, top] until
  // reset.  yZoomWin holds the active y sub-range (null = show the pinned fixed range); it's captured from
  // the drag in the setSelect hook and cleared on double-click.  (uPlot's own drag-scale can't do this here
  // because yScale.range hard-returns the fixed band, ignoring the value uPlot sets from the selection.)
  let yZoomWin = o.yInit || null;   // seed from a persisted y-zoom (o.yInit) so a zoom survives a re-render
  let markY = null, markYBot = null;
  if ((o.depMarks && o.depMarks.length) || (o.failMarks && o.failMarks.length)) {
    let gmax = -Infinity;
    S.forEach((s) => s.ys.forEach((v) => { if (v != null && isFinite(v) && v > gmax) gmax = v; }));
    if (gmax > 0) markY = gmax * (o.depTopMul || 2);
  }
  // Fixed axis: park the off-scale marks at the FIXED top-margin height (not 2x the current view's data),
  // so failed/OOM/dep marks stay separated at a consistent top regardless of the view (fixes both the
  // "not separated" and the "not at the top for n=4" cases).
  if (yFixR) { markY = yFixR.mark; markYBot = yFixR.markBot; }
  // TIME axes carry MINUTES, which reads badly below a minute: the denoiser history sat at 0.035 to
  // 0.055 min where 2.1 to 3.3 s is the natural way to say it.  o.yMinutes lets such a panel display
  // SECONDS instead.  The ticks are generated and formatted in the display unit and converted back to
  // minutes for their positions, so the scale, the zoom windows, the markers and the tooltips all stay
  // in minutes and only the axis changes.  The unit is chosen from the panel's DATA rather than from
  // the current scale, so a drag-zoom cannot leave the label saying one unit and the numbers another.
  let yMul = 1;
  if (o.yMinutes) {   // SECONDS_LABEL is the one spelling every panel uses (see its definition)
    let dmax = -Infinity;
    if (o.yRange && o.yRange[1] != null) dmax = o.yRange[1];
    else S.forEach((sp) => sp.ys.forEach((v) => { if (v != null && isFinite(v) && v > dmax) dmax = v; }));
    if (dmax > 0 && dmax < 1) yMul = 60;
  }
  const yUnitLabel = yMul === 60 ? SECONDS_LABEL : o.yLabelText;
  const series = [{}, ...S.map((s) => ({
    stroke: s.color, width: s.width == null ? 2 : s.width, dash: s.dash || undefined,
    spanGaps: true,  // bridge null cells (e.g. a failed non-dividing size) so the curve stays connected
    // psize 0 means "no markers" (e.g. the ideal line) — hide them via show:false.  Passing
    // size:0 makes uPlot compute a NEGATIVE arc radius and throw mid-draw, which aborted every
    // redraw and silently broke drag-zoom (the zoom's commit redraw never finished).
    points: { show: s.psize !== 0, size: s.psize == null ? 5 : s.psize, stroke: s.ring || s.color,
      fill: s.fillPoints ? s.color : ((s.pointsOnly || s.hollow) ? bg : s.color), width: s.pw == null ? 1 : s.pw },
    ...(s.pointsOnly ? { paths: () => null } : {}),
  }))];
  const xAxis = { scale: "x", stroke: axc, grid: { stroke: grc, width: 1 }, ticks: { stroke: grc, size: 4 },
    font: "11px " + (cs.fontFamily || "sans-serif") };
  // Custom ticks at the measured sizes/devices — but RANGE-AWARE: only the ticks inside the current
  // scale window.  (A fixed full list would keep out-of-range ticks after a zoom and fight uPlot's
  // ranging, which blocked drag-zoom.)  filter keeps all of them (uPlot's default log filter would
  // otherwise drop the non-power-of-10 ones, e.g. the 512³ tick).
  if (o.xSplits) { xAxis.splits = (u, ai, mn, mx) => o.xSplits.filter((v) => v >= mn && v <= mx); xAxis.filter = (u, sp) => sp; }
  if (o.xLabels) { xAxis.values = (u, sp) => sp.map((v) => o.xLabels[v] != null ? o.xLabels[v] : ""); }
  if (o.xLabelText) xAxis.label = o.xLabelText;
  const yAxis = { scale: "y", stroke: axc, grid: { stroke: grc, width: 1 }, ticks: { stroke: grc, size: 4 },
    font: "11px sans-serif", size: 52 };
  if (o.yLog) {
    yAxis.values = (u, sp) => logAxisValues(
      { min: u.scales.y.min * yMul, max: u.scales.y.max * yMul }, sp.map((v) => v * yMul));
    // Generate the log ticks ourselves (see logTicks) so uPlot's built-in log splitter — which can
    // hang for seconds on unclean tight bounds — never runs.  Mirrors the custom x-splits above;
    // filter:identity keeps every gridline (logAxisValues blanks the minor labels on a wide range).
    yAxis.splits = (u, ai, mn, mx) => logTicks(mn * yMul, mx * yMul).map((v) => v / yMul);
    yAxis.filter = (u, sp) => sp;
  } else if (o.yfmt) yAxis.values = (u, sp) => sp.map((v) => v == null ? "" : o.yfmt(v));
  if (yUnitLabel) yAxis.label = yUnitLabel;
  // uPlot's default log range snaps min/max OUT to the enclosing powers of 10.  On a drag-zoom that
  // expands the selection back out (a ~1-decade pick on the size axis snaps to the full 1e7..1e10
  // view), so you never get the region you dragged.  o.tightLog gives the log scales an identity
  // range — the scale is exactly its data extent (or the zoom selection), no power-of-10 rounding.
  // (It's a function, not a fixed array, so it still zooms/resets — unlike the old hard-clamp.)
  const tightLog = (u, mn, mx) => [mn, mx];
  // Pad a scale by `frac` of its span on each end — log-space for log scales, linear otherwise — to
  // give the panel a little breathing room at the edges (applies to the initial view and to zooms).
  // A ZERO-span input (e.g. a single-device CPU run whose speedup is always 1× -> min==max) must NOT
  // be returned as-is: uPlot's tick splitter then loops forever building an array (RangeError /
  // multi-second hang).  Open such a range up to a small non-zero window instead.
  const padRange = (frac, isLog) => (u, mn, mx) => {
    if (mn == null || mx == null) return [mn, mx];
    if (isLog) {
      if (!(mn > 0) || !(mx > 0)) return [mn, mx];           // non-positive: leave it to uPlot
      let a = Math.log10(mn), b = Math.log10(mx);
      if (b - a < 1e-9) { a -= 0.5; b += 0.5; } else { const p = (b - a) * frac; a -= p; b += p; }
      return [Math.pow(10, a), Math.pow(10, b)];
    }
    let lo = mn, hi = mx;
    if (hi - lo < 1e-12) { const d = Math.abs(lo) * 0.1 || 1; lo -= d; hi += d; }
    else { const p = (hi - lo) * frac; lo -= p; hi += p; }
    return [lo, hi];
  };
  const xScale = { distr: o.xLog ? 3 : 1, time: !!o.xTime };
  if (o.xRange) xScale.range = o.xRange;
  else if (o.padAll != null) xScale.range = padRange(o.padAll, o.xLog);
  else if (o.tightLog && o.xLog) xScale.range = tightLog;
  const yScale = { distr: o.yLog ? 3 : 1 };
  // Base y-range (data padding), then lift the top above any floating markers (markY) so they stay on
  // screen.  Composes with padRange so the scaling panels (yPad) also make room for off-scale fail marks.
  const baseYRange = o.yPad != null ? padRange(o.yPad, o.yLog)
                   : o.padAll != null ? padRange(o.padAll, o.yLog)
                   : (o.tightLog && o.yLog) ? tightLog : null;
  // A FIXED y-axis (yFixR, the History panels' all-time range) pins [bot, top] with proportional log
  // margins; the off-scale marks sit at yFixR.mark within the top margin.  Otherwise: auto data range,
  // lifting the top above any floating markers (markY) so they stay on screen.
  if (yFixR) yScale.range = () => yZoomWin || [yFixR.bot, yFixR.top];
  else if (markY != null || baseYRange) yScale.range = (u, mn, mx) => {
    let hi = mx;
    if (markY != null) hi = Math.max(hi, markY);
    if (baseYRange) return baseYRange(u, mn, hi);
    if (o.yLog) { const flo = (mn > 0) ? Math.pow(10, Math.floor(Math.log10(mn))) : mn; return [flo, hi * 1.25]; }
    return [Math.min(mn, 0), hi * 1.1];
  };
  else if (baseYRange) yScale.range = baseYRange;
  // Truly-nearest drawn point to the cursor, in 2-D px (shared by the click-to-pick and hover-tooltip
  // handlers).  Previously this snapped to uPlot's cursor.idx — the nearest x-COLUMN — then took the
  // nearest series in y.  When two runs sit close in x (two commits a few hours apart on a multi-day
  // axis), a few px of cursor motion flips the snapped column and the pick jumps to a far run.  Scanning
  // every drawn (series, column) by Euclidean px distance instead picks the dot the eye is closest to.
  const nearestPoint = (u, maxPx) => {
    const cl = u.cursor.left, ct = u.cursor.top;
    if (cl == null || ct == null || cl < 0 || ct < 0) return null;
    let bSi = -1, bDi = -1, bD = Infinity;
    for (let si = 1; si < u.data.length; si++) {
      const col = u.data[si];
      for (let di = 0; di < col.length; di++) {
        const v = col[di]; if (v == null) continue;
        const dx = u.valToPos(u.data[0][di], "x") - cl, dy = u.valToPos(v, "y") - ct;
        const d = dx * dx + dy * dy;
        if (d < bD) { bD = d; bSi = si; bDi = di; }
      }
    }
    return (bSi > 0 && bD <= maxPx * maxPx) ? { si: bSi, di: bDi } : null;
  };
  // Hover tooltip: o.tooltip(spec, idx) -> HTML for the nearest data point; o.depMarks / o.failMarks carry
  // their own pre-built tooltip HTML shown when the cursor is near their top-band symbol (checked first).
  let tip = null;
  if (o.tooltip || (o.depMarks && o.depMarks.length) || (o.failMarks && o.failMarks.length)) {
    tip = document.createElement("div"); tip.className = "u-tip";
  }
  const hooks = {};
  const placeTip = (u, html) => {                     // position the shared tip at the cursor, flipping near the right edge
    tip.innerHTML = html; tip.style.display = "block";
    const ob = u.over.getBoundingClientRect(), eb = el.getBoundingClientRect();
    let lx = ob.left - eb.left + u.cursor.left + 14;
    if (lx + tip.offsetWidth > el.clientWidth) lx = Math.max(0, ob.left - eb.left + u.cursor.left - tip.offsetWidth - 14);
    tip.style.left = lx + "px"; tip.style.top = (ob.top - eb.top + u.cursor.top + 8) + "px";
  };
  // nearest top-band marker (dep or fail) within ~10 CSS-px of the cursor, near the marker row, else null
  const nearestMark = (u, marks, bottomToo) => {
    if (!(marks && marks.length)) return null;
    const rows = [(markY != null) ? u.valToPos(markY, "y") : 12];   // the top marker row (floating, or fixed band)
    if (bottomToo) rows.push((markYBot != null) ? u.valToPos(markYBot, "y")
                             : u.bbox.height / (u.pxRatio || 1) - 12);   // bottom band (config/note markers live here even without markYBot)
    if (!rows.some((my) => Math.abs(u.cursor.top - my) <= 14)) return null;
    let best = null, bd = 10;
    marks.forEach((d) => { const dx = Math.abs(u.valToPos(d.x, "x") - u.cursor.left); if (dx < bd) { bd = dx; best = d; } });
    return best;
  };
  if (tip) hooks.setCursor = [(u) => {
    const cl = u.cursor.left, ct = u.cursor.top;
    // hide while off-plot or mid drag-zoom
    if (cl == null || ct == null || cl < 0 || ct < 0 || (u.select && u.select.width > 1)) { tip.style.display = "none"; return; }
    const fail = nearestMark(u, o.failMarks);           // a failure symbol wins (most urgent), then a dep symbol
    if (fail) { placeTip(u, fail.tip); return; }
    const dep = nearestMark(u, o.depMarks, true);
    if (dep) { placeTip(u, dep.tip); return; }
    if (!o.tooltip) { tip.style.display = "none"; return; }
    const np = nearestPoint(u, 30), oi = np ? np.di - padOff : -1;
    const html = (np && oi >= 0 && oi < xs.length) ? o.tooltip(specs[np.si - 1], oi) : null;
    if (!html) { tip.style.display = "none"; return; }
    placeTip(u, html);
  }];
  // Linked x-zoom: when one plot in a sync group zooms (or resets) its x-scale, mirror the range to
  // the others.  Propagate ONLY when a peer's range actually differs, so the cascade converges
  // instead of looping (uPlot commits setScale on rAF, so a sync flag wouldn't span the callbacks).
  if (o.syncX || o.onXChange) { const group = o.syncX || [];
    hooks.setScale = [(u, key) => {
      if (key !== "x") return;
      const mn = u.scales.x.min, mx = u.scales.x.max;
      group.forEach((p) => { if (p !== u && (p.scales.x.min !== mn || p.scales.x.max !== mx)) p.setScale("x", { min: mn, max: mx }); });
      if (o.onXChange) o.onXChange(mn, mx);   // report the (zoomed/reset) x-window so a caller can persist it
    }];
  }
  // Box-zoom capture (o.boxZoom + a fixed range): a drag with vertical extent zooms Y too, like the
  // scaling panels.  uPlot's built-in drag-scale can't move a hard-pinned axis (yScale.range ignores the
  // value it sets), so read the selection's y sub-range here and feed it back through yZoomWin; the pinned
  // range applies whenever yZoomWin is null.  select.top/height are CSS-px in the plot area; posToVal maps
  // them to data (top px = higher value on an upward y-axis).
  if (o.boxZoom && yFixR) {
    hooks.setSelect = [(u) => {
      const s = u.select;
      if (!s || s.height <= 6) return;                       // a pure horizontal drag / a click -> leave Y pinned
      const a = u.posToVal(s.top, "y"), b = u.posToVal(s.top + s.height, "y");
      if (isFinite(a) && isFinite(b) && a !== b) { yZoomWin = [Math.min(a, b), Math.max(a, b)]; if (o.onYChange) o.onYChange(yZoomWin); }
    }];
  }
  // Optional custom triangle marks at data points (o.marks: [{x, y}]) — uPlot only draws CIRCLE
  // points, so failing-test flags are painted as red triangles in a draw hook (canvas/device px).
  if (o.marks && o.marks.length) hooks.draw = [(u) => {
    const ctx = u.ctx, dpr = u.pxRatio || 1, h = 9 * dpr;   // ~match the 'ran hot' ring (psize 14)
    const L = u.bbox.left, R = u.bbox.left + u.bbox.width, T = u.bbox.top, B = u.bbox.top + u.bbox.height;
    ctx.save();
    o.marks.forEach((m) => {
      const x = u.valToPos(m.x, "x", true), y = u.valToPos(m.y, "y", true);
      if (!isFinite(x) || !isFinite(y)) return;
      // Cull marks outside the plot box, else valToPos maps an out-of-range point into the axis margin
      // and the glyph paints there (e.g. a test-fail triangle stranded left of a zoomed x-window, or above
      // a y-zoom).  The floating dep/fail hooks already cull this way.
      if (x < L - 1 || x > R + 1 || y < T - 1 || y > B + 1) return;
      if (m.shape === "x") {                                 // a CORRECTNESS divergence: a bold ✕ (white-haloed)
        const r = h * 1.15;
        const cross = () => { ctx.beginPath(); ctx.moveTo(x - r, y - r); ctx.lineTo(x + r, y + r);
                              ctx.moveTo(x + r, y - r); ctx.lineTo(x - r, y + r); ctx.stroke(); };
        ctx.lineCap = "round";
        ctx.lineWidth = 4 * dpr; ctx.strokeStyle = "#fff"; cross();        // halo for contrast
        ctx.lineWidth = 2.2 * dpr; ctx.strokeStyle = m.color || CORRC; cross();
      } else if (m.shape === "diamond") {                    // a dep-canary RE-MEASURE: a diamond in the series colour
        const r = h * 1.05;
        ctx.beginPath(); ctx.moveTo(x, y - r); ctx.lineTo(x + r, y); ctx.lineTo(x, y + r); ctx.lineTo(x - r, y); ctx.closePath();
        ctx.fillStyle = m.color || DEPC; ctx.fill();
        ctx.lineWidth = 1.5 * dpr; ctx.strokeStyle = "#fff"; ctx.stroke();  // white edge so it reads over the circle point
      } else {                                               // a FAILING-TESTS flag: a red triangle
        ctx.beginPath(); ctx.moveTo(x, y - h); ctx.lineTo(x - h, y + h); ctx.lineTo(x + h, y + h); ctx.closePath();
        ctx.fillStyle = m.color || FAILC; ctx.fill();
        ctx.lineWidth = 1.2 * dpr; ctx.strokeStyle = "#fff"; ctx.stroke();   // white edge for contrast
      }
    });
    ctx.restore();
  }];
  // "Run shown" overlay (o.showNow): a full-height red guide at the CURRENT run's commit-x, drawn in
  // drawClear so it sits BEHIND the grid + data + marks (never obscures them), plus a red ring around
  // that run's point(s) on its OWN platform's series, drawn just UNDER the ✕/throttle/test marks.  The
  // ring lands on the blue (GPU) or amber (CPU) line, so it also says which platform you're viewing.
  // currentRun() is read LIVE, so navigating runs only needs el._u.redraw() (no re-render -> zoom survives).
  if (o.showNow) {
    const drawNow = (u, ring) => {
      const cur = (typeof currentRun === "function") ? currentRun() : null;
      if (!cur) return;
      const nx = u.valToPos(runTime(cur), "x", true);
      if (!isFinite(nx)) return;
      const ctx = u.ctx, dpr = u.pxRatio || 1;
      if (!ring) {
        ctx.save(); ctx.strokeStyle = "rgba(163,45,45,0.55)"; ctx.lineWidth = 3 * dpr;
        ctx.beginPath(); ctx.moveTo(nx, u.bbox.top); ctx.lineTo(nx, u.bbox.top + u.bbox.height); ctx.stroke(); ctx.restore();
        return;
      }
      const di = X.indexOf(runTime(cur));
      if (di < 0) return;
      ctx.save(); ctx.strokeStyle = CORRC; ctx.lineWidth = 2 * dpr;
      S.forEach((s) => {
        if (!s.meta || s.pointsOnly || s.meta.platform !== cur.platform || s.meta.branch !== cur.branch) return;
        const v = s.ys[di]; if (v == null) return;
        const y = u.valToPos(v, "y", true); if (!isFinite(y)) return;
        ctx.beginPath(); ctx.arc(nx, y, 13 * dpr, 0, 2 * Math.PI); ctx.stroke();
      });
      ctx.restore();
    };
    hooks.drawClear = [(u) => drawNow(u, false)];
    hooks.draw = hooks.draw ? [(u) => drawNow(u, true), ...hooks.draw] : [(u) => drawNow(u, true)];
  }
  // Top-band Y for the floating markers (markY; a fixed band as fallback).  Shared by dep + fail glyphs.
  const bandY = (u, r) => { const t = u.bbox.top; const a = (markY != null) ? u.valToPos(markY, "y", true) : t + r + 3 * dpr; return isFinite(a) ? a : t + r + 3 * dpr; };
  // Bottom-band Y — mirror of bandY for the dep marker's second (bottom) glyph, which helps it stand out
  // when the middle of the plot is busy.  markYBot lives in the enlarged bottom margin (fixed-range panels
  // with dep marks only); a fixed offset above the axis is the fallback.
  const bandYBot = (u, r) => { const b = u.bbox.top + u.bbox.height; const a = (markYBot != null) ? u.valToPos(markYBot, "y", true) : b - r - 3; return isFinite(a) ? a : b - r - 3; };
  // Failed/OOM configs (o.failMarks: [{x, tip}]) — a red ⊗ (ring + ✕) FLOATING above all data with a faint
  // red drop-line, so an OOM reads as an off-scale ceiling breach, never as a real (low) value.  Hover ->
  // nearestMark tooltip.  Drawn first so point markers sit on top.
  if (o.failMarks && o.failMarks.length) {
    const drawFails = (u) => {
      const ctx = u.ctx, dpr = u.pxRatio || 1, bot = u.bbox.top + u.bbox.height;
      const L = u.bbox.left, R = u.bbox.left + u.bbox.width, r = 8 * dpr, my = bandY(u, r), q = r * 0.5;
      ctx.save();
      o.failMarks.forEach((d) => {
        const x = u.valToPos(d.x, "x", true);
        if (!isFinite(x) || x < L - 1 || x > R + 1) return;
        ctx.strokeStyle = FAILC; ctx.globalAlpha = 0.5; ctx.lineWidth = 1.5 * dpr;               // faint red drop-line
        ctx.setLineDash([5 * dpr, 4 * dpr]); ctx.beginPath(); ctx.moveTo(x, my + r); ctx.lineTo(x, bot); ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        ctx.beginPath(); ctx.arc(x, my, r, 0, 2 * Math.PI); ctx.fillStyle = bg; ctx.fill();       // ring
        ctx.lineWidth = 2 * dpr; ctx.strokeStyle = FAILC; ctx.stroke();
        ctx.lineWidth = 1.8 * dpr; ctx.beginPath();                                               // ✕ inside
        ctx.moveTo(x - q, my - q); ctx.lineTo(x + q, my + q); ctx.moveTo(x + q, my - q); ctx.lineTo(x - q, my + q); ctx.stroke();
      });
      ctx.restore();
    };
    hooks.draw = hooks.draw ? [drawFails, ...hooks.draw] : [drawFails];
  }
  // Dependency-change markers (o.depMarks: [{x, tip, color}]) — a green diamond in the TOP margin at each
  // dep-set onset (a torch bump / periodic deps refresh: a "ruler change" that shifts every series at once),
  // plus a SECOND green diamond in the bottom margin on the fixed-range time/mem panels (markYBot), the two
  // linked by a faint dashed drop-line.  The bottom glyph keeps the dep change visible when the middle of
  // the plot is dense with data.  Sized to match the on-curve re-measure diamonds.  No inline text — it
  // collided when two changes shared an x; the description lives in a hover tooltip (nearestMark, above,
  // which checks both bands).  Drawn FIRST in the chain so it sits UNDER the point markers/rings.
  if (o.depMarks && o.depMarks.length) {
    const drawDeps = (u) => {
      const ctx = u.ctx, dpr = u.pxRatio || 1, bot = u.bbox.top + u.bbox.height;
      const L = u.bbox.left, R = u.bbox.left + u.bbox.width, r = 9 * dpr, myT = bandY(u, r);
      const hasBot = markYBot != null;                                                          // bottom band = fixed-range time/mem panels
      const myB = hasBot ? bandYBot(u, r) : bot;
      const diamond = (x, my) => {                                                              // filled diamond, white-edged
        ctx.beginPath();
        ctx.moveTo(x, my - r); ctx.lineTo(x + r, my); ctx.lineTo(x, my + r); ctx.lineTo(x - r, my); ctx.closePath();
        ctx.fill(); ctx.stroke();
      };
      ctx.save();
      o.depMarks.forEach((d) => {
        const x = u.valToPos(d.x, "x", true);
        if (!isFinite(x) || x < L - 1 || x > R + 1) return;
        const botOnly = d.band === "bottom";                                                    // config/note markers: bottom band only
        const yB = botOnly ? bandYBot(u, r) : myB;                                              // bottom-only works even without markYBot (fallback above the axis)
        ctx.strokeStyle = d.color || DEPC; ctx.globalAlpha = 0.5; ctx.lineWidth = 1.5 * dpr;   // faint drop-line (the x-locator)
        ctx.setLineDash([5 * dpr, 4 * dpr]);
        ctx.beginPath();
        if (botOnly) { ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, yB - r); }
        else { ctx.moveTo(x, myT + r); ctx.lineTo(x, hasBot ? myB - r : bot); }
        ctx.stroke();
        ctx.setLineDash([]); ctx.globalAlpha = 1;
        ctx.fillStyle = d.color || DEPC; ctx.lineWidth = 1.5 * dpr; ctx.strokeStyle = bg;       // fill, white edge over the grid
        if (!botOnly) diamond(x, myT);                                                          // top band (dep changes, as before)
        if (botOnly || hasBot) diamond(x, botOnly ? yB : myB);                                  // bottom band
      });
      ctx.restore();
    };
    hooks.draw = hooks.draw ? [drawDeps, ...hooks.draw] : [drawDeps];
  }
  const opts = {
    width: o.width || el.clientWidth || 320, height: o.height || 210,
    scales: { x: xScale, y: yScale },
    series, axes: [xAxis, yAxis], legend: { show: false },
    // drag a region to zoom (uPlot built-in): 2-D box zoom on the scaling panels, x-only on the
    // time-series history panels.  Double-click resets.  (The fixed-array x-range that previously
    // hard-clamped the scale is gone — see xPad — so the drag actually takes now.)
    // drag.dist: a drag shorter than this (px) is NOT a zoom — it falls through to a plain click, so
    // a click with a pixel or two of jitter still selects the point instead of zooming a sliver.
    cursor: { points: { size: 7 }, drag: { x: true, y: o.boxZoom ? true : !o.xTime, dist: 6 } },
    hooks: (hooks.setCursor || hooks.setScale || hooks.setSelect || hooks.draw || hooks.drawClear) ? hooks : undefined,
  };
  if (el._u) { el._u.destroy(); el._u = null; }
  el.innerHTML = "";
  try { el._u = new uPlot(opts, data, el); } catch (e) { el.innerHTML = "<p class='muted'>chart error: " + e.message + "</p>"; return null; }
  if (tip) el.appendChild(tip);
  if (o.syncX) o.syncX.push(el._u);
  // Double-click resets uPlot's scales; drop any captured y-zoom in the SAME gesture so the pinned range
  // returns.  Capture phase so this runs BEFORE uPlot's own dblclick reset re-ranges the axis.
  if (o.boxZoom && yFixR) {
    const ov = el.querySelector(".u-over");
    if (ov) ov.addEventListener("dblclick", () => { yZoomWin = null; if (o.onYChange) o.onYChange(null); }, true);
  }
  // Optional: a plain click (vs a drag, which zooms) selects the nearest point.
  if (o.onPick) {
    const over = el.querySelector(".u-over");
    if (over) over.addEventListener("click", () => {
      const u = el._u; const np = nearestPoint(u, 40);
      if (!np) return;
      const oi = np.di - padOff;
      if (oi >= 0 && oi < xs.length) o.onPick(specs[np.si - 1], oi);
    });
  }
  return el._u;
}

// ---- header / selectors ------------------------------------------------------
function goOptions() {
  const run = latestRun(ui_state.platform, ui_state.branch);
  if (!run) return [];
  const combos = uniq(run.cells.map((c) => c.geom + "|" + c.op));
  return combos.sort((a, b) => {
    const [ga, oa] = a.split("|"), [gb, ob] = b.split("|");
    return GEOM_ORDER.indexOf(ga) - GEOM_ORDER.indexOf(gb) || OP_ORDER.indexOf(oa) - OP_ORDER.indexOf(ob);
  });
}
function syncGoSelect() {
  const opts = goOptions();
  if (!opts.includes(ui_state.go)) ui_state.go = opts.includes("cone|vcd_nonconst") ? "cone|vcd_nonconst" : opts[0];
  fillSelect("op", opts, ui_state.go, opts.map((s) => s.replace("|", " · ")));
}

// ---- tiles + drill-down ------------------------------------------------------
// Headline numbers for one platform's run of the selected branch (null if none).
function platMetrics(plat) {
  const run = runOnPlat(plat);
  if (!run) return null;
  return { run,
    cells: run.cells.length,   // TOTAL configs attempted (matches status_nightly's "cells N");
                               // failures are surfaced via cellsFailed (the sub + drill-down + red markers)
    cellsFailed: run.cells.filter((c) => c.failed).length,
    gate: run.gate.hard.length,
    correctness: runCorrCells(run),   // distinct divergent configs (own severity tier)
    perfHard: runPerfHard(run).length,  // hard hits that are NOT correctness (memory / structural / ok->fail)
    // The authoritative count is the pytest SUMMARY's `failed` (what status_nightly uses).  The
    // `failures` node-id LIST is only for the drill-down and can be empty if the log format hid the
    // names (e.g. pytest-xdist without -ra), so counting it would under-report (showed 0 vs 3).
    testsFailed: run.tests ? (run.tests.failed || 0) : 0,
    testsPassed: run.tests ? run.tests.passed : null };
}
function renderTiles() {
  const box = $("tiles");
  const cur = currentRun();
  if (!cur) { box.innerHTML = "<p class='muted'>no runs.</p>"; return; }
  // The first three tiles always show BOTH platforms (cpu + gpu) for the selected branch.
  const mets = {}; M.platforms.forEach((p) => mets[p] = platMetrics(p));
  const anyBad = (isBad) => M.platforms.some((p) => mets[p] && isBad(mets[p]));
  const pvs = (pick, isBad) => `<div class="pvs">` + M.platforms.map((p) => {
    const m = mets[p];
    if (!m) return `<span class="pv none" title="no ${p.toUpperCase()} run for this commit">${p.toUpperCase()}<b>—</b></span>`;
    return `<span class="pv">${p.toUpperCase()}<b class="${isBad(m) ? "bad" : ""}">${pick(m)}</b></span>`;
  }).join("") + `</div>`;
  const health = [
    { id: "cells", lbl: "configs measured", body: pvs((m) => m.cells, (m) => m.cellsFailed > 0),
      click: anyBad((m) => m.cellsFailed > 0), sub: anyBad((m) => m.cellsFailed > 0) ? "failures — click" : "all ran" },
    { id: "correctness", lbl: "correctness", body: pvs((m) => m.correctness, (m) => m.correctness > 0),
      click: true, sub: anyBad((m) => m.correctness > 0) ? "DIVERGENT — click" : "fingerprints match" },
    { id: "gate", lbl: "performance regressions", body: pvs((m) => m.perfHard, (m) => m.perfHard > 0),
      click: true, sub: "click for details" },
    { id: "tests", lbl: "tests failed", body: pvs((m) => m.testsFailed, (m) => m.testsFailed > 0),
      click: anyBad((m) => m.testsFailed > 0), sub: anyBad((m) => m.testsFailed > 0) ? "failures — click" : "none failing" },
  ];
  // Flags for the shown run: tint the tile + ⚠ badge(s).  Failing tests and throttling are RED
  // (warn-throttled); merely running hot is amber (warn-hot).  Both badges show if both apply.
  const therm = runThermal(cur);
  const tf = (cur.tests && cur.tests.failed) || 0;
  const corr = runCorrCells(cur);
  const corrAlert = runAlert(cur);          // incorrect AND not acknowledged -> dominant red
  const corrAcked = corr > 0 && !corrAlert; // incorrect but acknowledged -> muted note, audit trail
  const sev = (tf || (therm && therm.sev === "throttled")) ? "throttled" : (therm ? "hot" : null);
  // Correctness OUTRANKS perf/thermal: an unacknowledged INCORRECT run is red regardless of the rest.
  const warnTile = corrAlert ? " incorrect" : (sev ? ` warn-${sev}` : "");
  const warnLine =
    (corrAlert ? `<div class="warnflag incorrect">⚠ INCORRECT — ${corr} divergent config${corr > 1 ? "s" : ""}</div>`
       : corrAcked ? `<div class="warnflag muted">✓ ${corr} correctness divergence${corr > 1 ? "s" : ""} (acknowledged)</div>` : "")
    + (therm ? `<div class="warnflag ${therm.sev}">⚠ ${therm.sev === "throttled" ? "throttled" : "ran hot"} · n=${therm.ndevs.join(", ")}${therm.peak ? ` · up to ${therm.peak}°C` : ""}</div>` : "")
    + (tf ? `<div class="warnflag throttled">⚠ ${tf} test${tf > 1 ? "s" : ""} failed</div>` : "");
  // Run navigation (this tile): ◀/▶ step through THIS platform+branch's runs by commit time; the ⇄
  // toggle jumps to the OTHER platform's run of the SAME commit (greyed if that platform never measured
  // this commit).  Both keep the open drill-down so you can step and watch the same panel update.
  const series = runsFor(ui_state.platform, ui_state.branch).slice().sort((a, b) => runTime(a) - runTime(b));
  const idx = series.findIndex((r) => runKey(r) === runKey(cur));   // cur, not ui_state.runKey (null until you navigate)
  const prevR = idx > 0 ? series[idx - 1] : null;
  const nextR = (idx >= 0 && idx < series.length - 1) ? series[idx + 1] : null;
  const otherPlat = M.platforms.find((p) => p !== ui_state.platform);
  const otherR = (otherPlat && cur.commit_full)
    ? (runsFor(otherPlat, ui_state.branch).filter((r) => r.commit_full === cur.commit_full)
         .sort((a, b) => runTime(b) - runTime(a))[0] || null)
    : null;
  const runTile =
    `<div class="tile${warnTile}" data-click="false">
       <div class="lbl">run shown</div>
       <div class="runnav">
         <button class="rn-step" data-dir="prev" ${prevR ? "" : "disabled"} title="${prevR ? "older run · " + commitMinute(prevR) : "oldest run"}">◀</button>
         <button class="rn-step" data-dir="next" ${nextR ? "" : "disabled"} title="${nextR ? "newer run · " + commitMinute(nextR) : "newest run"}">▶</button>
         ${idx >= 0 && series.length > 1 ? `<span class="rn-pos">${idx + 1}/${series.length}</span>` : ""}
         <button class="rn-plat" ${otherR ? "" : "disabled"} title="${otherR ? "same commit on " + (otherPlat || "").toUpperCase() : "no " + (otherPlat || "other").toUpperCase() + " run for this commit"}">⇄&nbsp;${(otherPlat || "").toUpperCase()}</button>
       </div>
       <div class="when">${commitMinute(cur)}</div>
       <div class="sub"><b>${ui_state.branch}</b> · <b>${platLabel(ui_state.platform).toUpperCase()}</b> · ${cur.commit}${cur.dirty ? " · dirty" : ""}</div>
       ${warnLine}
     </div>`;
  box.innerHTML = health.map((t) =>
    `<div class="tile ${t.click ? "click" : ""} ${ui_state.openTile === t.id ? "open" : ""}" data-id="${t.id}" data-click="${!!t.click}">
       <div class="lbl">${t.lbl}</div>${t.body}<div class="sub">${t.sub}</div></div>`
  ).join("") + runTile;
  box.querySelectorAll(".tile").forEach((el) => {
    if (el.dataset.click === "true") el.onclick = () => {
      ui_state.openTile = ui_state.openTile === el.dataset.id ? null : el.dataset.id;
      renderTiles(); renderDetail();
    };
  });
  // Run-shown nav: ◀/▶ step within the series, ⇄ swaps to the other platform's same-commit run.
  box.querySelectorAll(".rn-step").forEach((b) => b.onclick = () => showRun(b.dataset.dir === "prev" ? prevR : nextR));
  const platBtn = box.querySelector(".rn-plat");
  if (platBtn) platBtn.onclick = () => showRun(otherR);
}
// The per-cell "vs <reference>" correctness detail for ONE run — the SAME blocks used in the main
// #detail panel and (inline) in the correctness banner, so the two can never drift.  Findings grouped
// by cell; within a cell, one block per reference (prior / main / single-device / other-platform).
function corrDetailBlocks(run) {
  const fs = runCorr(run);
  if (!fs.length) return `<p class="muted">fingerprints match the references — no divergence.</p>`;
  const byCell = {};
  fs.forEach((f) => { (byCell[f.cell] = byCell[f.cell] || []).push(f); });
  return Object.keys(byCell).map((cell) => {
    const refs = byCell[cell].map((f) =>
      `<div class="vsref">vs ${f.basis}</div><ul class="discr">${f.discrepancies.map((d) => `<li>${d}</li>`).join("")}</ul>`).join("");
    return `<div class="hitcell"><div class="hitcoords">${cellCoords(cell)}</div>${refs}</div>`;
  }).join("");
}
function renderDetail() {
  const box = $("detail");
  if (!ui_state.openTile) { box.innerHTML = ""; return; }
  const titles = { gate: "Performance regressions — hard-gate hits", correctness: "Correctness — fingerprint divergences",
                   tests: "Failing tests", cells: "Failed configs" };
  const pct = (v) => v == null ? "?" : v + "%";
  // The drill-down covers BOTH platforms (matching the tiles).  Gate/correctness get a one-time
  // threshold explanation since the thresholds are identical across platforms.
  let intro = "";
  const anyRun = M.platforms.map(runOnPlat).find(Boolean);
  const gc = (anyRun && anyRun.gate_config) || {};
  if (ui_state.openTile === "gate") {
    intro = `<p>Each run is compared per config + metric against its reference run(s).  This panel shows <b>performance</b> regressions — correctness has its own tile. <b>Hard:</b> structural change, ok→fail, expected-but-absent, GPU peak-memory &gt;${pct(gc.mem_hard_pct)}. <b>Soft:</b> speedup drop &gt;${pct(gc.speedup_warn_pct)}, time &gt;${pct(gc.time_soft_pct)}, CPU memory, sweep add/drop.</p>`;
  } else if (ui_state.openTile === "correctness") {
    const ct = M.corr_tol || {};
    intro = `<p>Correctness compares the recon <b>fingerprint</b> against four references — the <b>prior run</b> on this branch, the latest <b>main</b>, <b>single-device n=1</b> within the same run, and the <b>other platform</b> (CPU↔GPU) at the same commit. Flags a float64 {sum, mean, l2norm} relative change beyond ${ct.single ?? "?"} (single-shot) / ${ct.iter ?? "?"} (iterated VCD) / ${ct.xdev ?? "?"} (cross-device) / ${ct.xplat ?? "?"} (cross-platform), or a shape/dtype change.${M.cleared_through ? " Divergences on commits dated ≤ " + M.cleared_through + " are acknowledged." : ""}</p>`;
  }
  const section = (plat) => {
    const run = runOnPlat(plat);
    const head = `<h4>${plat.toUpperCase()}${run ? ` · ${run.commit} · ${commitMinute(run)}` : ""}</h4>`;
    if (!run) return head + `<p class="muted">no ${plat.toUpperCase()} run for this commit.</p>`;
    if (ui_state.openTile === "correctness") {
      return head + corrDetailBlocks(run);   // shared with the banner's inline expansion
    }
    if (ui_state.openTile === "gate") {
      const hits = runPerfHard(run);
      const cmp = (run.gate.compared_to || []).map((c) => priorLabel(c, plat)).join(", ") || "its reference run(s)";
      if (!hits.length) return head + `<p class="muted">no performance regressions (result: ${run.gate.result || "?"}).</p>`;
      // Group hits by cell: one coords line + a bullet per discrepancy.
      const byCell = {};
      hits.forEach((h) => { const k = h.cell || "—"; (byCell[k] = byCell[k] || []).push(h); });
      const blocks = Object.keys(byCell).map((cell) => {
        const coords = cell === "—" ? "(run-level)" : cellCoords(cell);
        const bullets = byCell[cell].map((h) => `<li>${h.detail || h.text}</li>`).join("");
        return `<div class="hitcell"><div class="hitcoords">${coords}</div><ul class="discr">${bullets}</ul></div>`;
      }).join("");
      return head + `<p class="muted">vs ${cmp}</p>${blocks}`;
    }
    if (ui_state.openTile === "tests") {
      const t = run.tests, f = (t && t.failures) || [];
      return head + (f.length ? `<ul>${f.map((x) => `<li class="bad">${x}</li>`).join("")}</ul>`
        : (t && t.failed ? `<p class="bad">${t.failed} failing</p><p class="muted">(test names not captured in this log)</p>`
        : `<p class="muted">${t ? t.passed + " passed, none failing" : "no test log"}.</p>`));
    }
    const f = run.cells.filter((c) => c.failed);  // "cells"
    return head + (f.length ? `<ul>${f.map((c) => `<li class="bad">${cellKey(c)}${c.oom ? " — OOM" : ""}${c.error ? " — " + c.error : ""}</li>`).join("")}</ul>`
      : `<p class="muted">all configs ran.</p>`);
  };
  box.innerHTML = `<div class="detail-box"><h3>${titles[ui_state.openTile] || ""}</h3>${intro}${M.platforms.map(section).join("")}</div>`;
}

// ---- scaling view: data ------------------------------------------------------
function gridFor(run, geom, op) {
  const cs = run.cells.filter((c) => c.geom === geom && c.op === op);
  const sizes = uniq(cs.map((c) => c.size)).sort((a, b) => sizeVol(a) - sizeVol(b));
  const ndevs = uniq(cs.map((c) => c.ndev)).sort((a, b) => a - b);
  const at = (s, n) => cs.find((c) => c.size === s && c.ndev === n) || null;
  return { sizes, ndevs, at, cells: cs };
}
function refVal(geom, op, size, nd, metric) {
  const key = `${geom}|${op}|${size}|${nd}`;
  if (ui_state.ref === "best") { const r = M.records[ui_state.platform + "|" + ui_state.branch]; const e = r && r[key]; return e && e[metric] ? e[metric].value : null; }
  const run = refRun(); if (!run) return null;
  const c = findCell(run, key);
  return c ? c[metric] : null;
}
// reference overlay series for the absolute (vs-size) panels.  No device-count restriction: a
// reference run carries whatever device counts it measured (main is n=1-only, prerelease shards
// parallel, etc.), and refVal returns null where the ref lacks a cell -> spanGaps bridges it.
function refSeries(geom, op, sizes, ndevs, metric, div) {
  if (ui_state.ref === "none") return [];
  const lab = REF_LABEL[ui_state.ref] || ui_state.ref;
  const out = [];
  ndevs.forEach((nd) => {
    const ys = sizes.map((s) => { const v = refVal(geom, op, s, nd, metric); return v != null ? v / div : null; });
    if (ys.some((y) => y != null)) out.push({ label: `${lab} n=${nd}`, color: REFC, ys, width: 4, fillPoints: true, psize: 4 });
  });
  return out;
}
// red-ring markers for hard-gate cells of this op, on the panel whose metric matches
function gateSeries(run, geom, op, sizes, metricWord, div) {
  const hits = run.gate.hard.filter((h) => h.cell && h.cell.startsWith(geom + "|" + op + "|") && (h.text || "").toLowerCase().includes(metricWord));
  if (!hits.length) return null;
  const bySize = {};   // size -> {ndev, detail, basis} of the failing cell, for a rich hover tooltip
  hits.forEach((h) => { const p = h.cell.split("|"); bySize[p[2]] = { ndev: +p[3], detail: h.detail, basis: h.basis }; });
  const field = metricWord === "memory" ? "mem_mb" : "min_ms";
  const ys = sizes.map((s) => {
    if (!(s in bySize)) return null;
    const c = findCell(run, geom + "|" + op + "|" + s + "|" + bySize[s].ndev);
    return c && c[field] != null ? c[field] / div : null;
  });
  return ys.some((y) => y != null) ? { label: "gate fail", color: FAILC, ys, pointsOnly: true, psize: 9, pw: 3, isGate: true, bySize } : null;
}
// Thermal markers on cells whose timing may be suspect — two tiers per device curve: a filled amber
// disc where the driver actually throttled (causal), a hollow amber ring where a GPU merely ran hot
// (advisory).  (A hot GPU gates the slowest-device multi-GPU timing, so a 2x jump with no code change
// is usually this, not a regression.)
function throttleSeries(run, geom, op, sizes, ndevs, field, div) {
  const out = [];
  ndevs.forEach((nd) => {
    const cells = sizes.map((s) => findCell(run, geom + "|" + op + "|" + s + "|" + nd));
    const val = (c) => (c && !c.failed && c[field] != null) ? c[field] / div : null;
    // confirmed throttle (clocks clamped) -> filled amber disc; ran hot only -> hollow amber ring
    const thr = cells.map((c) => (c && cellThrottled(c)) ? val(c) : null);
    const hot = cells.map((c) => (c && cellHot(c) && !cellThrottled(c)) ? val(c) : null);
    if (thr.some((y) => y != null)) out.push({ label: "throttled n=" + nd, color: THROTC, ys: thr, pointsOnly: true, fillPoints: true, psize: 12, pw: 2.5 });
    if (hot.some((y) => y != null)) out.push({ label: "hot n=" + nd, color: THROTC, ys: hot, pointsOnly: true, hollow: true, psize: 14, pw: 2.5 });
  });
  return out;
}

function renderScaling() {
  const run = currentRun();
  const [geom, op] = ui_state.go.split("|");
  $("sv-meta").textContent = run ? `${geom} · ${op} — ${ui_state.branch} @ ${run.commit} · ${commitMinute(run)}` : "";
  if (ui_state.view === "table") { $("sv-plot").style.display = "none"; $("sv-table").style.display = ""; renderScalingTable(run, geom, op); return; }
  $("sv-plot").style.display = ""; $("sv-table").style.display = "none";
  const g = gridFor(run, geom, op);
  const { sizes, ndevs, at } = g;
  if (!sizes.length) { $("pTime").innerHTML = "<p class='muted'>no cells.</p>"; return; }
  const PLAT = (ui_state.platform || "").toUpperCase();
  // The ∝-voxels/views "ideal" reference doesn't hold for the translation geometry — suppress it
  // (line + caption note) there; keep it for parallel/cone/multiaxis.
  const showIdeal = geom !== "translation";
  $("capTime").textContent = `${PLAT}: time vs size${showIdeal ? ` · ideal ∝ ${IDEAL_BASIS[op] || "voxels"}` : ""}`;
  $("capMem").textContent = `${PLAT}: memory vs size${showIdeal ? " · ideal ∝ voxels" : ""}`;
  $("capSpeed").textContent = `${PLAT}: speedup vs devices`;
  $("capShard").textContent = `${PLAT}: per-device memory ÷ sino shard`;
  const xvol = sizes.map(sizeVol);
  // Tick labels at the measured sizes — but collapse near-identical volumes
  // (e.g. 512³ vs the non-dividing 513³, <1% apart) to one tick so both the
  // small and large ends get a readable label instead of colliding.
  const xticks = [], xLabels = {};
  sizes.forEach((s) => { const v = sizeVol(s); if (!xticks.length || v / xticks[xticks.length - 1] > 1.03) xticks.push(v); xLabels[v] = s; });
  // Pad the log x-range so the smallest/largest ticks don't sit on the axes
  // (their labels would otherwise be clipped at the panel edges).
  const w = $("pTime").clientWidth || 460;
  // Hover tooltips: a rich cell readout (config + result) over a measured curve; a plain label+value
  // over the ideal/reference/gate overlays.  `fb` formats the overlay's y for the panel's unit.
  const cellLine = (c) => `<span class="tdim">time</span> ${c.min_ms != null ? fmtTime(c.min_ms / 60000) : "—"}`
    + `<br><span class="tdim">peak mem</span> ${fmtGB(c.mem_mb)}`
    + (c.speedup != null ? `<br><span class="tdim">speedup</span> ${c.speedup.toFixed(2)}×` : "")
    + (cellHot(c) ? `<br><span class="thr">⚠ ${hotWarn(c)}</span>` : "");
  const sizeTip = (fb, metricWord) => (spec, idx) => {
    const size = sizes[idx];
    if (spec.isGate) {   // the "gate fail" overlay marker: the SAME full cell readout as a normal point + the gate context
      const g = spec.bySize[size]; if (!g) return null;
      const c = at(size, g.ndev);
      const head = `<b>${geom} · ${op}</b> · ${size} · n=${g.ndev}`;
      const body = c ? (c.failed ? `<span class="bad">${c.oom ? "OOM" : "FAILED"}</span>` : cellLine(c)) : "";
      return `${head}${body ? "<br>" + body : ""}<br>${gateNote(g.detail, g.basis)}`;
    }
    const m = /n=(\d+)/.exec(spec.label || "");
    if (!m || spec.color === REFC) { const y = spec.ys[idx]; return y == null ? null : `<b>${geom} · ${op}</b> · ${size}<br>${spec.label}: ${fb(y)}`; }
    const c = at(size, +m[1]); if (!c) return null;
    const head = `<b>${geom} · ${op}</b> · ${size} · n=${m[1]}`;
    if (c.failed) return `${head}<br><span class="bad">${c.oom ? "OOM" : "FAILED"}</span>${c.error ? " — " + c.error : ""}`;
    // if this exact cell is itself a hard-gate hit for this panel's metric, append the gate context too, so
    // it shows whether the cursor snaps to the overlay marker or the underlying curve
    const gh = (run.gate.hard || []).find((h) => h.cell === `${geom}|${op}|${size}|${m[1]}` && (h.text || "").toLowerCase().includes(metricWord));
    return `${head}<br>${cellLine(c)}${gh ? "<br>" + gateNote(gh.detail, gh.basis) : ""}`;
  };
  // Per-(size, n) derived metrics for the two device-axis panels — computed once so each panel's curve
  // and its hover tooltip read the SAME number.  speedup = (n=1 time / this time) × the base n;
  // mem÷shard = measured peak ÷ the ideal even sinogram shard (sizeVol·4 bytes ÷ n).
  const speedupAt = (s, nd) => { const c = at(s, nd), base = at(s, ndevs[0]);
    return (c && base && !c.failed && !base.failed && c.min_ms) ? (base.min_ms / c.min_ms) * ndevs[0] : null; };
  const shardAt = (s, nd) => { const c = at(s, nd); if (!c || c.failed || c.mem_mb == null) return null;
    return c.mem_mb / ((sizeVol(s) * 4 / nd) / (1024 * 1024)); };
  // Shared by BOTH device-axis panels (speedup and mem÷shard): show both derived metrics — not just the
  // one this panel plots — alongside the raw time/peak-mem, so either panel gives the full readout.
  const devTip = (spec, idx) => {
    const nd = ndevs[idx], size = spec.label;
    if (!/^\d+x\d+x\d+$/.test(size)) { const y = spec.ys[idx]; return y == null ? null : `n=${nd}<br>${spec.label}: ${fmtNum(y)}`; }
    const c = at(size, nd); if (!c) return null;
    const head = `<b>${geom} · ${op}</b> · ${size} · n=${nd}`;
    if (c.failed) return `${head}<br><span class="bad">${c.oom ? "OOM" : "FAILED"}</span>`;
    const sp = speedupAt(size, nd), sh = shardAt(size, nd);
    return `${head}<br><span class="tdim">time</span> ${c.min_ms != null ? fmtTime(c.min_ms / 60000) : "—"}`
      + `<br><span class="tdim">peak mem</span> ${fmtGB(c.mem_mb)}`
      + `<br><span class="tdim">speedup</span> ${sp != null ? sp.toFixed(2) + "×" : "—"}`
      + `<br><span class="tdim">mem ÷ shard</span> ${sh != null ? sh.toFixed(1) + "×" : "—"}`
      + (cellHot(c) ? `<br><span class="thr">⚠ ${hotWarn(c)}</span>` : "");
  };

  // anchor the ideal at the fastest measured point (smallest size, most devices)
  const fastN = ndevs[ndevs.length - 1], aV = xvol[0];
  const aT = at(sizes[0], fastN), aM = at(sizes[0], fastN);

  // --- time vs size (log-log, minutes) ---
  // uPlot paints series[1] LAST (on top), so order matters: gate markers + the
  // reference go FIRST (low index → drawn on top), current curves + ideal after,
  // otherwise the reference hides behind a near-coincident current curve.
  const texp = IDEAL_EXP[op] != null ? IDEAL_EXP[op] : 1;
  const timeCurves = ndevs.map((nd) => ({ label: "n=" + nd, color: devColor(nd),
    ys: sizes.map((s) => { const c = at(s, nd); return c && !c.failed && c.min_ms != null ? c.min_ms / 60000 : null; }) }));
  const timeIdeal = (showIdeal && aT && aT.min_ms != null) ? [{ label: "ideal", color: IDEAL, dash: [5, 4], width: 1.5, psize: 0,
    ys: xvol.map((v) => (aT.min_ms / 60000) * Math.pow(v / aV, texp)) }] : [];
  const gT = gateSeries(run, geom, op, sizes, "time", 60000);
  // Failed/OOM configs -> off-scale red ⊗ markers ABOVE all curves (one per failing SIZE), NOT interpolated
  // onto the curve (which read as a real, often-lower value — e.g. an OOM landing below a slower n=4).
  // Shared by the time + memory panels (same failing configs).
  const sizeFailMk = sizes.map((s, i) => {
    const fails = ndevs.map((nd) => at(s, nd)).filter((c) => c && c.failed);
    return fails.length ? { x: xvol[i], tip: `<b>failed at ${s}</b><br>` + fails.map((c) => `n=${c.ndev} — <span class="bad">${c.oom ? "OOM" : "FAILED"}</span>${c.error ? " · " + c.error : ""}`).join("<br>") } : null;
  }).filter(Boolean);
  const timeSpecs = [...(gT ? [gT] : []), ...throttleSeries(run, geom, op, sizes, ndevs, "min_ms", 60000), ...refSeries(geom, op, sizes, ndevs, "min_ms", 60000), ...timeCurves, ...timeIdeal];
  linePlot($("pTime"), xvol, timeSpecs, { width: w, xLog: true, yLog: true, tightLog: true, yPad: 0.06, xSplits: xticks, xLabels, xPad: 1.7, yLabelText: "minutes", yMinutes: true, tooltip: sizeTip(fmtTime, "time"), failMarks: sizeFailMk, depTopMul: 1.5 });

  // --- memory vs size (log-log, GB) ---  (same draw-order rule as the time panel)
  const memCurves = ndevs.map((nd) => ({ label: "n=" + nd, color: devColor(nd),
    ys: sizes.map((s) => { const c = at(s, nd); return c && !c.failed && c.mem_mb != null ? c.mem_mb / 1024 : null; }) }));
  const memIdeal = (showIdeal && aM && aM.mem_mb != null) ? [{ label: "ideal", color: IDEAL, dash: [5, 4], width: 1.5, psize: 0,
    ys: xvol.map((v) => (aM.mem_mb / 1024) * (v / aV)) }] : [];
  const gM = gateSeries(run, geom, op, sizes, "memory", 1024);
  const memSpecs = [...(gM ? [gM] : []), ...throttleSeries(run, geom, op, sizes, ndevs, "mem_mb", 1024), ...refSeries(geom, op, sizes, ndevs, "mem_mb", 1024), ...memCurves, ...memIdeal];
  linePlot($("pMem"), xvol, memSpecs, { width: w, xLog: true, yLog: true, tightLog: true, yPad: 0.06, xSplits: xticks, xLabels, xPad: 1.7, yLabelText: "GB", tooltip: sizeTip((y) => y.toFixed(2) + " GB", "memory"), failMarks: sizeFailMk, depTopMul: 1.5 });

  // --- speedup vs devices (one curve per size; ideal linear) ---
  const w2 = $("pSpeed").clientWidth || 460;
  const speedCurves = sizes.map((s, i) => ({ label: s, color: SIZEC[i % SIZEC.length],
    ys: ndevs.map((nd) => speedupAt(s, nd)) }));
  const speedIdeal = ndevs.slice();
  // Failed configs -> off-scale markers per failing DEVICE COUNT (shared by speedup + shard).  depTopMul is
  // smaller here (linear axes) so the marker floats just above the data instead of doubling the range.
  const devFailMk = ndevs.map((nd) => {
    const fails = sizes.map((s) => at(s, nd)).filter((c) => c && c.failed);
    return fails.length ? { x: nd, tip: `<b>failed at n=${nd}</b><br>` + fails.map((c) => `${c.size} — <span class="bad">${c.oom ? "OOM" : "FAILED"}</span>`).join("<br>") } : null;
  }).filter(Boolean);
  const speedSpecs = [...speedCurves, { label: "ideal", color: IDEAL, dash: [5, 4], width: 1.5, psize: 0, ys: speedIdeal }];
  linePlot($("pSpeed"), ndevs, speedSpecs, { width: w2, padAll: 0.07, xSplits: ndevs, xLabels: Object.fromEntries(ndevs.map((n) => [n, String(n)])), yfmt: (v) => v.toFixed(0) + "×", yLabelText: "speedup", xLabelText: "devices", tooltip: devTip, failMarks: devFailMk, depTopMul: 1.5 });

  // --- per-device memory ÷ sino shard (one curve per size; ideal 2x) ---
  const shardCurves = sizes.map((s, i) => ({ label: s, color: SIZEC[i % SIZEC.length],
    ys: ndevs.map((nd) => shardAt(s, nd)) }));
  const shardSpecs = [...shardCurves, { label: "ideal 2×", color: IDEAL, dash: [5, 4], width: 1.5, psize: 0, ys: ndevs.map(() => 2) }];
  linePlot($("pShard"), ndevs, shardSpecs, { width: w2, padAll: 0.07, xSplits: ndevs, xLabels: Object.fromEntries(ndevs.map((n) => [n, String(n)])), yfmt: (v) => v.toFixed(1) + "×", yLabelText: "mem ÷ shard", xLabelText: "devices", tooltip: devTip, failMarks: devFailMk, depTopMul: 1.5 });

  renderScalingLegend(ndevs, sizes, showIdeal);
}
function renderScalingLegend(ndevs, sizes, showIdeal) {
  const k = (c, t, dash) => `<span class="k"><span class="sw" style="background:${c};${dash ? "height:0;border-top:2px dashed " + c : ""}"></span>${t}</span>`;
  // failed/OOM config = a red ⊗ pinned ABOVE the curves (off-scale) — an OOM has no real value, so it
  // never sits on a curve where it could read as a fast time.  Hover it for which config failed.
  const ringDot = (t) => `<span class="k"><span class="cx" style="color:${FAILC}">⊗</span>${t}</span>`;
  const devs = ndevs.map((n) => k(devColor(n), "n=" + n)).join("");
  const szs = sizes.map((s, i) => k(SIZEC[i % SIZEC.length], s)).join("");
  // active comparison: solid black swatch + display name + provenance (branch @ commit)
  const refNote = ui_state.ref !== "none"
    ? `<span class="k"><span class="sw" style="background:${REFC};height:4px"></span>${REF_LABEL[ui_state.ref] || ui_state.ref}${refProvenance() ? " (" + refProvenance() + ")" : ""}</span>` : "";
  // Top legend sits above time & memory (device-count curves + the overlay ref);
  // the second legend sits above speedup & shard (size curves).
  $("sv-legend").innerHTML =
    `<span class="grp">${devs}</span>` +
    `<span class="grp">${showIdeal ? k(IDEAL, "ideal", true) : ""}${ringDot("failed / OOM (off scale)")}<span class="k"><span class="ring"></span>gate fail</span><span class="k"><span class="ring" style="border-color:${THROTC}"></span>ran hot</span><span class="k"><span class="dot" style="background:${THROTC}"></span>throttled</span>${refNote}</span>`;
  $("sv-legend2").innerHTML =
    `<span class="grp">${szs}</span>` +
    `<span class="grp">${k(IDEAL, "ideal", true)}${ringDot("failed / OOM (off scale)")}</span>`;
}

function renderScalingTable(run, geom, op) {
  const g = gridFor(run, geom, op);
  const { sizes, ndevs, at } = g;
  const box = $("sv-table");
  if (!sizes.length) { box.innerHTML = "<p class='muted'>no cells.</p>"; return; }
  // pick one time unit for the whole table by the max time
  const allMs = g.cells.filter((c) => !c.failed && c.min_ms != null).map((c) => c.min_ms);
  const useMin = allMs.length && Math.max(...allMs) >= 60000;
  const tUnit = useMin ? "min" : "s", tDiv = useMin ? 60000 : 1000;
  const fmtT = (ms) => ms == null ? "—" : (ms / tDiv).toFixed(2);
  const refActive = ui_state.ref !== "none";
  const dCell = (cur, ref, lowerBetter) => {
    if (cur == null || ref == null || ref === 0) return "<td class='num'>—</td>";
    const d = ((cur - ref) / Math.abs(ref)) * 100;
    if (Math.abs(d) < 1) return `<td class='num'>${d > 0 ? "+" : ""}${d.toFixed(1)}%</td>`;
    const worse = lowerBetter ? d > 0 : d < 0;
    return `<td class='num ${worse ? "up" : "dn"}'>${d > 0 ? "+" : ""}${d.toFixed(1)}%</td>`;
  };
  const tbl = (title, field, div, fmt, unit) => {
    let h = `<table class='grid'><caption>${title}${unit ? " (" + unit + ")" : ""}${refActive ? " · Δ vs " + ui_state.ref : ""}</caption><thead><tr><th>devices</th>`;
    sizes.forEach((s) => { h += `<th>${s}</th>`; if (refActive) h += "<th>Δ</th>"; });
    h += "</tr></thead><tbody>";
    ndevs.forEach((nd) => {
      h += `<tr><td class='l'>n=${nd}</td>`;
      sizes.forEach((s) => {
        const c = at(s, nd);
        if (c && c.failed) { h += `<td class='fail-cell'>${c.oom ? "OOM" : "FAIL"}</td>`; if (refActive) h += "<td></td>"; return; }
        const cur = c ? c[field] : null;
        h += `<td class='num'>${cur == null ? "—" : fmt(cur)}</td>`;
        if (refActive) h += dCell(cur, refVal(geom, op, s, nd, field), true);
      });
      h += "</tr>";
    });
    return h + "</tbody></table>";
  };
  box.innerHTML = tbl("time", "min_ms", tDiv, fmtT, tUnit) + tbl("memory", "mem_mb", 1024, fmtGB, "");
}

// ---- history strip -----------------------------------------------------------
// Headline-op time + peak memory at each geometry's LARGEST size, for device count `n` (gate count
// is n-independent).  `geoms` is the active group; `timeOp` is its headline op (vcd for cone/parallel,
// back for translation/multiaxis — they don't run vcd).  Sizes differ per geometry now, so the
// "largest size" is computed PER geometry (a global max would miss the smaller new geometries).
function aggregate(run, n, geoms, timeOp) {
  // Keep the source cell behind each point (timeCell/memCell) so the history can flag GPU
  // throttling — the amber ring + tooltip warning — exactly as the scaling panels do.
  const out = { time: {}, mem: {}, timeCell: {}, memCell: {}, gate: run.gate.hard.length,
                gatePerf: runPerfHard(run).length,   // perf-only hard count (correctness is its own signal)
                testsFailed: (run.tests && run.tests.failed) || 0,
                corrAlert: runAlert(run) };   // unacknowledged correctness divergence -> ✕ marker
  geoms.forEach((gm) => {
    const gmSizes = run.cells.filter((c) => c.geom === gm).map((c) => c.size);
    if (!gmSizes.length) { out.time[gm] = out.mem[gm] = out.timeCell[gm] = out.memCell[gm] = null; return; }
    const focus = Math.max(...gmSizes.map(sizeVol));
    const focusSize = gmSizes.find((s) => sizeVol(s) === focus);
    // The HEADLINE op at the largest size drives BOTH time and memory (op-consistent), so an OOM leaves
    // both null instead of letting memory fall back to a surviving op and read as an improvement.  The
    // failure itself is flagged run-level by the off-scale ⊗ (failMk), which reads r.cells directly.
    const tc = run.cells.find((c) => c.geom === gm && c.op === timeOp && c.size === focusSize && c.ndev === n && !c.failed);
    out.time[gm] = tc && tc.min_ms != null ? tc.min_ms / 60000 : null;
    out.mem[gm] = tc && tc.mem_mb != null ? tc.mem_mb / 1024 : null;
    out.timeCell[gm] = out.memCell[gm] = tc || null;
  });
  return out;
}
// Click a history point -> show that run (and switch platform/branch to match).
// Load a specific run into the run-dependent views (tiles / detail / scaling), syncing the platform +
// branch selectors.  History is left untouched so its zoom survives.  By default the open drill-down is
// PRESERVED (so the run-shown ◀/▶ can step while you watch the same panel update); pass resetOpen=true to
// close it (History clicks do, since you're navigating away from a specific point).
// Re-highlight the "run shown" guide/ring on the History panels WITHOUT re-rendering them (the draw
// hooks read currentRun() live, so a redraw() suffices) — so the marker tracks navigation while the
// panels' zoom survives.
function refreshHistoryNow() {
  ["hVcd", "hMem", "hGate", "hVcdT", "hMemT", "hGateT", "poTime", "poMem"].forEach((id) => { const el = $(id); if (el && el._u) el._u.redraw(); });
}
function showRun(r, resetOpen) {
  if (!r) return;
  ui_state.platform = r.platform; ui_state.branch = r.branch; ui_state.runKey = runKey(r);
  if (resetOpen) ui_state.openTile = null;
  fillSelect("platform", M.platforms, ui_state.platform, platLabels());
  fillSelect("branch", branchesFor(ui_state.platform), ui_state.branch);
  renderTiles(); renderDetail(); syncGoSelect(); renderScaling(); refreshHistoryNow();
}
function pickRun(spec, idx) {
  const t = spec._xs[idx];
  showRun(runsFor(spec.meta.platform, spec.meta.branch).find((x) => runTime(x) === t), true);
}
// Tooltip HTML for a dep-change marker: one block per dep_info entry (a canary step), ordered by gen.
// Shows the step (torch bump / deps refresh), the torch version delta, and the per-package changes
// when the build recorded them (a run without them shows just the torch delta from the toolchain).
function depTipHtml(entries) {
  const head = (e) => e.reason === "deps-step" ? "deps refresh"
                    : e.reason === "torch-step" ? "torch bump"
                    : e.reason === "code-step" ? "code + new torch" : "dep change";
  const parts = entries.slice().sort((a, b) => (a.gen || 0) - (b.gen || 0)).map((e) => {
    // Prefer "available X, installed Y" when a newer torch shipped but the install resolved to an
    // older one (held back by the env Python floor or a pyproject exclusion); else the installed-
    // version delta between generations; else the bare installed version.
    let torch;
    if (e.torch_available && e.torch_to && e.torch_available !== e.torch_to)
      torch = `torch ${e.torch_available} available, installed ${e.torch_to}`;
    else if (e.torch_from && e.torch_from !== e.torch_to)
      torch = `torch ${e.torch_from} → ${e.torch_to}`;
    else
      torch = e.torch_to ? `torch ${e.torch_to}` : "";
    let s = `<b>${head(e)}</b>${torch ? ` · ${torch}` : ""}`;
    const ch = e.pkg_changes || [];
    if (ch.length) {
      s += "<br>" + ch.map((c) => `<span class="tdim">${c.name}</span> ${c.from || "∅"}→${c.to || "∅"}`).join("<br>");
      if ((e.pkg_n || ch.length) > ch.length) s += `<br><span class="tdim">+${e.pkg_n - ch.length} more</span>`;
    } else if (e.reason === "deps-step") {
      s += `<br><span class="tdim">no per-package deltas recorded</span>`;
    }
    return s;
  });
  return `<b>dependency change</b><br>${parts.join('<br><span class="tdim">———</span><br>')}`;
}
// Tooltip HTML for a config-change marker: the per-field default diffs recorded by the build
// (run.cfg_info), e.g. "partition_sequence: [4, 7] → [0, 2, 4, 6, 7]".
function cfgTipHtml(entries) {
  const fmtV = (v) => Array.isArray(v) ? `[${v.join(", ")}]` : String(v);
  const lines = entries.flatMap((e) => (e.changes || []).map(
    (c) => `<span class="tdim">${c.field}</span> ${fmtV(c.from)} → ${fmtV(c.to)}`));
  return `<b>library default change</b><br>${lines.join("<br>")}`;
}
// Config-change + hand-written-note markers (purple diamonds, same top band as dep changes).
// cfg_info comes from the build's consecutive-run policy diff; notes come from
// results/annotations.yaml (M.annotations: {date, text, platform?, branch?}).  ``plat`` filters
// to one platform (the per-op section); null shows all (History).
function cfgChangeMarks(plat) {
  const groups = {};
  M.runs.forEach((r) => {
    if (!r.cfg_info || (plat && r.platform !== plat)) return;
    const x = runTime(r), key = r.platform + "|" + Math.floor(x / 86400);
    const g = groups[key] || (groups[key] = { x: Infinity, entries: [] });
    g.x = Math.min(g.x, x);
    g.entries.push(r.cfg_info);
  });
  const marks = Object.values(groups).map((g) => ({ x: g.x, color: CFGC, band: "bottom", tip: cfgTipHtml(g.entries) }));
  (M.annotations || []).forEach((a) => {
    if (plat && a.platform && a.platform !== plat) return;
    const x = Date.parse(a.date) / 1000;
    if (!isFinite(x)) return;
    marks.push({ x, color: CFGC, band: "bottom", tip: `<b>note</b>${a.branch ? ` · ${a.branch}` : ""}<br>${a.text}` });
  });
  return marks;
}
// Persisted history x-window [min, max] so the time range survives branch/geom/run/group re-renders
// until a double-click reset (which restores the full range) or a page refresh.  null = auto/full.
let histXWindow = null;
// Per-panel y-zoom windows for the History time/mem panels, persisted across re-renders the same way
// histXWindow persists the x-window — so a y-zoom survives a branch/device change.  Reset when the
// geometry GROUP changes (its fixed y-range differs, so another group's zoom wouldn't map). null = full.
let histYWin = { time: null, mem: null };
let histYWinGroup = null;
function renderHistory() {
  // The history spans BOTH platforms and all branches; x is commit time
  // (falls back to collection date for older runs).
  const xs = uniq(M.runs.map(runTime)).sort((a, b) => a - b);
  const n = ui_state.histN;
  const group = HIST_GROUPS.find((g) => g.id === ui_state.histGroup) || HIST_GROUPS[0];
  if (group.id !== histYWinGroup) { histYWin = { time: null, mem: null }; histYWinGroup = group.id; }
  // Branch filter: "all" shows every branch (colour=platform, style=geometry); selecting one
  // restricts all three panels to that branch only.
  const branches = (ui_state.histBranch && ui_state.histBranch !== "all")
    ? M.branches.filter((b) => b === ui_state.histBranch) : M.branches;
  // One row of panels, covering every platform.  The shared sync group keeps the x-zoom
  // mirrored across the three panels.
  const histGroup = [];
  drawRow(M.platforms);

  function drawRow(rowPlats) {
  $("hCapVcd").textContent = `${group.opLabel} time at largest size (n=${n})`;
  $("hCapMem").textContent = `peak memory at largest size (n=${n})`;
  const aggByPB = {};  // "platform|branch" -> runTime -> aggregate (for the active geometry group)
  M.runs.forEach((r) => { const key = r.platform + "|" + r.branch; (aggByPB[key] = aggByPB[key] || {})[runTime(r)] = aggregate(r, n, group.geoms, group.op); });

  // colour = platform, line-style = geometry (one solid + one dashed per group — see GEOM_DASH).
  const cellField = (pick) => (pick === "time" ? "timeCell" : "memCell");
  const specsFor = (pick) => {
    const out = [], markers = [];
    rowPlats.forEach((plat) => branches.forEach((b) => group.geoms.forEach((gm) => {
      const agg = aggByPB[plat + "|" + b]; if (!agg) return;
      const ys = xs.map((t) => { const a = agg[t]; return a && a[pick][gm] != null ? a[pick][gm] : null; });
      if (!ys.some((y) => y != null)) return;
      const meta = { platform: plat, branch: b, geom: gm, pick };
      out.push({ label: `${plat} ${gm}`, color: PLATC[plat] || IDEAL,
        dash: GEOM_DASH[gm], ys, _xs: xs, meta });
      // two-tier thermal markers, re-derived client-side like the scaling panels: a filled amber disc
      // where the driver throttled (causal), a hollow amber ring where a GPU merely ran hot (advisory).
      const valAt = (t) => { const a = agg[t]; return a ? a[pick][gm] : null; };
      const cellAt = (t) => { const a = agg[t]; return a ? a[cellField(pick)][gm] : null; };
      const thr = xs.map((t) => { const c = cellAt(t); return (c && cellThrottled(c)) ? valAt(t) : null; });
      const hot = xs.map((t) => { const c = cellAt(t); return (c && cellHot(c) && !cellThrottled(c)) ? valAt(t) : null; });
      if (thr.some((y) => y != null)) markers.push({ label: `throttled ${plat} ${gm}`, color: THROTC,
        ys: thr, _xs: xs, meta, pointsOnly: true, fillPoints: true, psize: 12, pw: 2.5 });
      if (hot.some((y) => y != null)) markers.push({ label: `hot ${plat} ${gm}`, color: THROTC,
        ys: hot, _xs: xs, meta, pointsOnly: true, hollow: true, psize: 14, pw: 2.5 });
    })));
    // markers FIRST -> lowest series index -> drawn on top and win the hover tie at a flagged point
    return [...markers, ...out];
  };
  // With a single point, uPlot's auto time-range sprawls across years; pin a ±12h window.
  const DAY = 86400;
  const xr = xs.length < 2 ? [xs[0] - DAY / 2, xs[0] + DAY / 2] : null;
  // Pad the time domain by 5% of its span on each end so the first/last run isn't on the boundary
  // (where a click misses) — proportional, so it scales as the history grows instead of a fixed day.
  const xpad = xs.length > 1 ? (xs[xs.length - 1] - xs[0]) * 0.05 : 0;
  // Capture a zoom/reset so the window PERSISTS across re-renders: a zoom is a sub-range; a reset
  // (double-click) restores a range spanning all data -> forget the window (revert to auto/full).
  const histOnX = (min, max) => {
    histXWindow = (min != null && max != null && min <= xs[0] && max >= xs[xs.length - 1]) ? null : [min, max];
  };
  // hover tooltip: the same identity as the "run shown" tile (branch · platform · commit date+time).
  const histTip = (spec, idx) => {
    const r = runsFor(spec.meta.platform, spec.meta.branch).find((x) => runTime(x) === spec._xs[idx]);
    if (!r) return null;
    const y = spec.ys[idx], m = spec.meta;
    const a = m.geom ? (aggByPB[m.platform + "|" + m.branch] || {})[spec._xs[idx]] : null;
    // Value section: on the time/memory panels (geom-bearing) show BOTH time and peak memory for the
    // hovered run+platform+geom (the two panels share the same runs, so it's handy to read both at once);
    // on the gate panel (no geom) show the bare count.
    let valSection;
    if (m.geom && a) {
      const tv = a.time[m.geom], mv = a.mem[m.geom];
      valSection = `<br><span class="tdim">${m.platform} ${m.geom}</span>`
        + `<br><span class="tdim">time</span> ${fmtTime(tv)}`
        + `<br><span class="tdim">mem</span> ${mv != null ? fmtNum(mv) + " GB" : "—"}`;
    } else {
      const unit = m.pick === "mem" ? " GB" : m.pick === "time" ? " min" : "";
      valSection = (y != null ? `<br><span class="tdim">${spec.label}</span> ${fmtNum(y)}${unit}` : "");
    }
    // thermal warn: a hot GPU drags timing — flag if the time OR memory cell ran hot at this point.
    let warn = "";
    if (m.geom && a) {
      const tc = a.timeCell[m.geom], mc = a.memCell[m.geom];
      const hc = (tc && cellHot(tc)) ? tc : ((mc && cellHot(mc)) ? mc : null);
      if (hc) warn = `<br><span class="thr">⚠ ${hotWarn(hc)}</span>`;
    }
    // correctness divergence flag — the most severe, shown first (same source as the ✕ marker / tile).
    const corrN = runCorrCells(r);
    const corrWarn = corrN ? `<br><span class="corr">✕ ${corrN} correctness divergence${corrN > 1 ? "s" : ""}${runAcked(r) ? " (acknowledged)" : ""}</span>` : "";
    // tests-failed flag — same info as the run-shown tile's red badge.
    const tf = (r.tests && r.tests.failed) || 0;
    const testWarn = tf ? `<br><span class="bad">⚠ ${tf} test${tf > 1 ? "s" : ""} failed</span>` : "";
    // toolchain / dependency-canary: show the torch version (when recorded), and flag a dep-canary
    // re-measure — a point that shares its commit but was re-run on a newer dep set (see runTime).
    const torchNote = r.torch
      ? `<br><span class="tdim">torch</span> ${r.torch}${r.dep_gen ? ` · dep-canary re-measure (gen ${r.dep_gen})` : ""}`
      : "";
    return `<b>${r.branch}</b><br>${platLabel(r.platform).toUpperCase()} · ${commitMinute(r)}`
      + `<br><span class="tdim">commit</span> ${r.commit}${r.dirty ? " · dirty" : ""}`
      + torchNote + valSection + warn + corrWarn + testWarn;
  };
  // All history plots share one x (commit time) through the hoisted sync group, so dragging any
  // panel re-ranges every panel to the same window.
  // Red triangle on a run that had FAILING TESTS (a run-level flag, not per-cell like the thermal
  // markers): one per (platform, branch) failing run, sat on its drawn point for the panel's metric.
  // run-level overlay marks sat on the run's drawn point for the panel's metric: a red triangle for
  // FAILING TESTS, a bold ✕ for an unacknowledged CORRECTNESS divergence (flag = the aggregate field).
  const runMarks = (pick, flag, extra) => {
    const out = [];
    rowPlats.forEach((plat) => branches.forEach((b) => {
      const agg = aggByPB[plat + "|" + b]; if (!agg) return;
      xs.forEach((t) => {
        const a = agg[t]; if (!a || !a[flag]) return;
        let y = null;
        for (const gm of group.geoms) { if (a[pick][gm] != null) { y = a[pick][gm]; break; } }
        if (y != null) out.push({ x: t, y, ...extra });
      });
    }));
    return out;
  };
  // Dependency-canary re-measure glyphs: a violet diamond on every dep_gen>0 run's drawn point — a
  // re-measure of an existing commit on a NEWER dep set, distinct from a normal commit circle.
  const depDiamonds = (pick) => {
    const out = [];
    rowPlats.forEach((plat) => branches.forEach((b) => {
      const agg = aggByPB[plat + "|" + b]; if (!agg) return;
      runsFor(plat, b).forEach((r) => {
        if (!r.dep_gen) return;
        const a = agg[runTime(r)]; if (!a) return;
        let y = null;
        for (const gm of group.geoms) { if (a[pick][gm] != null) { y = a[pick][gm]; break; } }
        // Fixed dep-change violet (not the platform colour) so it matches the rule + legend — the
        // platform is already unambiguous from WHICH line the diamond sits on (cf. the amber thermal marks).
        if (y != null) out.push({ x: runTime(r), y, shape: "diamond", color: DEPC });
      });
    }));
    return out;
  };
  // Dependency-change markers: one top-band diamond per (platform, DAY) — the torch-step + deps-step of a
  // single canary night share a day and collapse into ONE marker whose tooltip lists both steps (grouping
  // per-day is what killed the old overlapping text labels).  Derived from the runs: gen>0 exists only on
  // the canary branch, but a dep change is ENVIRONMENTAL, so the marker shows across all series.  x = the
  // earliest re-measure time that day; the tooltip HTML is built from each run's build-time dep_info.
  const depChanges = () => {
    const groups = {};
    M.runs.forEach((r) => {
      if (!r.dep_gen || !rowPlats.includes(r.platform)) return;
      const x = runTime(r), key = r.platform + "|" + Math.floor(x / 86400);
      const g = groups[key] || (groups[key] = { x: Infinity, entries: [] });
      g.x = Math.min(g.x, x);
      g.entries.push(r.dep_info || { reason: r.run_reason, gen: r.dep_gen, torch_to: r.torch, torch_available: r.torch_available, pkg_changes: [], pkg_n: 0 });
    });
    return Object.values(groups).map((g) => ({ x: g.x, color: DEPC, tip: depTipHtml(g.entries) }));
  };
  const depMk = depChanges().concat(cfgChangeMarks(null));
  // ANY failed/OOM config in a run -> one off-scale red ⊗ at that run's x (run-level, like the tests /
  // correctness marks), with a tooltip listing every failed (geom, op, size, n).  Where the headline
  // largest-size cell is the one that OOM'd, its time+mem are null too (see aggregate), so the glyph is
  // the only signal there — an OOM never masquerades as a fast time or a memory win.
  const failMk = (() => {
    const byX = {};
    rowPlats.forEach((plat) => branches.forEach((b) => {
      runsFor(plat, b).forEach((r) => {
        (r.cells || []).forEach((c) => {
          if (!c.failed) return;
          (byX[runTime(r)] = byX[runTime(r)] || { x: runTime(r), cells: [] }).cells.push({ plat, c });
        });
      });
    }));
    return Object.values(byX).map((g) => {
      const one = g.cells.length === 1;   // a single failure -> room to show its error message inline
      const lines = g.cells.slice(0, 10).map(({ plat, c }) =>
        `${plat} ${c.geom} · ${c.op} · ${c.size} · n=${c.ndev} — <span class="bad">${c.oom ? "OOM" : "FAILED"}</span>${one && c.error ? " · " + c.error : ""}`);
      const more = g.cells.length > 10 ? `<br><span class="tdim">+${g.cells.length - 10} more</span>` : "";
      return { x: g.x, tip: `<b>failed config${g.cells.length > 1 ? "s" : ""}</b><br>${lines.join("<br>")}${more}` };
    });
  })();
  const allMarks = (pick) => [...depDiamonds(pick), ...runMarks(pick, "testsFailed", {}), ...runMarks(pick, "corrAlert", { shape: "x", color: CORRC })];
  // boxZoom: drag with vertical extent zooms Y too (like History by op); a pure horizontal drag still
  // zooms only the date range, and syncX keeps the x-window mirrored across time+mem.  (hGate below has
  // no fixed y-range, so it stays x-only.)
  const opts = (yl) => ({ xTime: true, xRange: xr, xPadAdd: xpad, yLog: true, yLabelText: yl, yfmt: fmtNum, onPick: pickRun, tooltip: histTip, syncX: histGroup, onXChange: histOnX, showNow: true, depMarks: depMk, boxZoom: true });
  const yr = (M.hist_yranges || {})[group.id] || {};   // fixed per-(geom-group, metric) y-axis (build-time)
  linePlot($("hVcd"), xs, specsFor("time"), { width: $("hVcd").clientWidth || 320, ...opts("min"), yMinutes: true, marks: allMarks("time"), failMarks: failMk, yRange: yr.time, yInit: histYWin.time, onYChange: (w) => { histYWin.time = w; } });
  linePlot($("hMem"), xs, specsFor("mem"), { width: $("hMem").clientWidth || 320, ...opts("GB"), marks: allMarks("mem"), failMarks: failMk, yRange: yr.mem, yInit: histYWin.mem, onYChange: (w) => { histYWin.mem = w; } });
  const gateSpecs = [];
  rowPlats.forEach((plat) => branches.forEach((b) => {
    const agg = aggByPB[plat + "|" + b]; if (!agg) return;
    const ys = xs.map((t) => { const a = agg[t]; return a ? a.gatePerf : null; });
    if (ys.some((y) => y != null)) gateSpecs.push({ label: `${plat} ${b}`, color: PLATC[plat] || IDEAL, ys, _xs: xs, meta: { platform: plat, branch: b } });
  }));
  linePlot($("hGate"), xs, gateSpecs, { width: $("hGate").clientWidth || 320, xTime: true, xRange: xr, xPadAdd: xpad, yLabelText: "count", yfmt: (v) => v.toFixed(0), onPick: pickRun, tooltip: histTip, syncX: histGroup, onXChange: histOnX, showNow: true, depMarks: depMk });
  // Re-open at the persisted window (if any) via setScale — NOT a clamped xRange — so drag-zoom and the
  // double-click reset keep working; syncX mirrors it to the other two panels.
  if (histXWindow && $("hVcd")._u) $("hVcd")._u.setScale("x", { min: histXWindow[0], max: histXWindow[1] });

  const k = (c, t, dash) => `<span class="k"><span class="sw" style="background:${c};${dash ? "height:0;border-top:2px dashed " + c : ""}"></span>${t}</span>`;
  // Only surface the dep-canary legend entries once a dep change has actually been recorded (gen>0 runs),
  // so a corpus that predates the canary doesn't carry unexplained keys.
  const depLeg = depMk.length
    ? `<span class="k"><span style="display:inline-block;width:8px;height:8px;background:${DEPC};transform:rotate(45deg);margin:0 4px"></span>dep change (hover ◆)</span>`
    : "";
  const failLeg = failMk.length
    ? `<span class="k"><span class="cx" style="color:${FAILC}">⊗</span>failed config (hover)</span>`
    : "";
  // Two fixed rows: platforms + geometries on line 1 (these change with the geom group), the symbol keys
  // on line 2 (constant) — so switching groups never reflows the symbols and the plots don't jump.
  $("hist-legend").innerHTML =
    `<span class="hl-line">` +
      `<span class="grp">${rowPlats.map((p) => k(PLATC[p] || IDEAL, platLabel(p))).join("")}</span>` +
      `<span class="grp">${group.geoms.map((gm) => k("#888", `${GEOM_LABEL[gm]} (${GEOM_DASH[gm] ? "dashed" : "solid"})`, !!GEOM_DASH[gm])).join("")}</span>` +
    `</span>` +
    `<span class="hl-line">` +
      `<span class="k"><span class="ring" style="border-color:${THROTC}"></span>ran hot</span>` +
      `<span class="k"><span class="dot" style="background:${THROTC}"></span>throttled</span>` +
      `<span class="k"><span class="tri" style="border-bottom-color:${FAILC}"></span>tests failed</span>` +
      depLeg + failLeg +
      `<span class="k"><span class="ring" style="border-color:${CORRC}"></span>run shown</span>` +
      `<span class="k"><span class="cx" style="color:${CORRC}">✕</span>incorrect</span>` +
    `</span>`;
  }
}

// ---- per-op history ----------------------------------------------------------
// A finer complement to the aggregated headline strip above: one time + one memory panel for a SINGLE
// (platform, geom|op, sinogram shape, device count), plotted over commit time with ONE LINE PER BRANCH
// (platform + op are fixed by the selectors, so the branch is what varies).  The y-axis is pinned per
// (platform, geom, op) — spanning all branches/shapes/n/runs (M.perop_yranges) — so flipping the shape
// or branch doesn't rescale it.  The section has its own branch filter (all + each branch, like History).
//
// Dep-change top-band markers for ONE platform (this section is single-platform).  Mirrors the per-day
// grouping in renderHistory's depChanges(), filtered to `plat`.
function depChangeMarks(plat) {
  const groups = {};
  M.runs.forEach((r) => {
    if (!r.dep_gen || (plat && r.platform !== plat)) return;
    const x = runTime(r), key = Math.floor(x / 86400);
    const g = groups[key] || (groups[key] = { x: Infinity, entries: [] });
    g.x = Math.min(g.x, x);
    g.entries.push(r.dep_info || { reason: r.run_reason, gen: r.dep_gen, torch_to: r.torch, torch_available: r.torch_available, pkg_changes: [], pkg_n: 0 });
  });
  return Object.values(groups).map((g) => ({ x: g.x, color: DEPC, tip: depTipHtml(g.entries) }));
}
// Selector option lists (cascading: platform -> geom|op -> shape -> n), each drawn from the runs on the
// chosen platform so an option always has data behind it.
function poOpOptions(plat) {
  return uniq(M.runs.filter((r) => r.platform === plat).flatMap((r) => r.cells.map((c) => c.geom + "|" + c.op)))
    .sort((a, b) => { const [ga, oa] = a.split("|"), [gb, ob] = b.split("|");
      return GEOM_ORDER.indexOf(ga) - GEOM_ORDER.indexOf(gb) || OP_ORDER.indexOf(oa) - OP_ORDER.indexOf(ob); });
}
function poShapeOptions(plat, geom, op) {
  return uniq(M.runs.filter((r) => r.platform === plat)
    .flatMap((r) => r.cells.filter((c) => c.geom === geom && c.op === op).map((c) => c.size)))
    .sort((a, b) => sizeVol(a) - sizeVol(b));
}
function poNOptions(plat, geom, op, shape) {
  return uniq(M.runs.filter((r) => r.platform === plat)
    .flatMap((r) => r.cells.filter((c) => c.geom === geom && c.op === op && c.size === shape).map((c) => c.ndev)))
    .filter((x) => x != null).sort((a, b) => a - b);
}
// Clamp the per-op selectors to valid values and (re)fill them.  Cascades: an invalid downstream pick
// (e.g. a shape the newly-chosen op never ran) falls back to a sensible default (largest shape, fewest n).
function syncPerOpSelectors() {
  if (!M.platforms.includes(ui_state.poPlat)) ui_state.poPlat = M.platforms.includes("gpu") ? "gpu" : (ui_state.platform || M.platforms[0]);
  fillSelect("poPlat", M.platforms, ui_state.poPlat, platLabels());
  const ops = poOpOptions(ui_state.poPlat);
  if (!ops.includes(ui_state.poGo)) ui_state.poGo = ops.includes("cone|vcd_nonconst") ? "cone|vcd_nonconst" : ops[0];
  fillSelect("poOp", ops, ui_state.poGo, ops.map((s) => s.replace("|", " · ")));
  const [geom, op] = (ui_state.poGo || "|").split("|");
  const shapes = poShapeOptions(ui_state.poPlat, geom, op);
  if (!shapes.includes(ui_state.poShape)) ui_state.poShape = shapes.length ? shapes[shapes.length - 1] : null;   // default = largest
  fillSelect("poShape", shapes, ui_state.poShape);
  const ns = poNOptions(ui_state.poPlat, geom, op, ui_state.poShape);
  if (!ns.includes(ui_state.poN)) ui_state.poN = ns.length ? ns[0] : 1;
  fillSelect("poN", ns, ui_state.poN);
}
// Persisted x-window for the per-op panels (independent of the history strip's histXWindow).
let poXWindow = null;
function renderPerOp() {
  syncPerOpSelectors();
  const plat = ui_state.poPlat, [geom, op] = (ui_state.poGo || "|").split("|");
  const shape = ui_state.poShape, n = ui_state.poN, PLAT = (plat || "").toUpperCase();
  $("poCapTime").textContent = `${PLAT}: ${geom} · ${op} time · ${shape || "—"} · n=${n}`;
  $("poCapMem").textContent = `${PLAT}: ${geom} · ${op} peak memory · ${shape || "—"} · n=${n}`;
  const branches = (ui_state.poBranch && ui_state.poBranch !== "all")
    ? M.branches.filter((b) => b === ui_state.poBranch) : M.branches;
  // x = commit times of EVERY run on this platform; a branch's curve is defined only at its own runs'
  // times (null elsewhere -> spanGaps bridges), exactly like the history strip.
  const xs = uniq(M.runs.filter((r) => r.platform === plat).map(runTime)).sort((a, b) => a - b);
  if (!xs.length || !shape) {
    $("poTime").innerHTML = "<p class='muted'>no runs for this selection.</p>"; $("poMem").innerHTML = ""; $("po-legend").innerHTML = ""; return;
  }
  const cellOf = (r) => r.cells.find((c) => c.geom === geom && c.op === op && c.size === shape && c.ndev === n) || null;
  const byBranch = {};   // branch -> { runTime -> cell }
  branches.forEach((b) => { const m = {}; runsFor(plat, b).forEach((r) => { m[runTime(r)] = cellOf(r); }); byBranch[b] = m; });

  const specsFor = (pick) => {
    const field = pick === "time" ? "min_ms" : "mem_mb", div = pick === "time" ? 60000 : 1024;
    const out = [], markers = [];
    branches.forEach((b) => {
      const map = byBranch[b];
      const val = (t) => { const c = map[t]; return c && !c.failed && c[field] != null ? c[field] / div : null; };
      const ys = xs.map(val);
      if (!ys.some((y) => y != null)) return;
      const meta = { platform: plat, branch: b, geom, op, size: shape, ndev: n, pick };
      out.push({ label: b, color: branchColor(b), ys, _xs: xs, meta });
      // two-tier thermal markers, like the history strip: filled amber disc = driver throttled, hollow ring = ran hot.
      const thr = xs.map((t) => { const c = map[t]; return (c && cellThrottled(c)) ? val(t) : null; });
      const hot = xs.map((t) => { const c = map[t]; return (c && cellHot(c) && !cellThrottled(c)) ? val(t) : null; });
      if (thr.some((y) => y != null)) markers.push({ label: "throttled " + b, color: THROTC, ys: thr, _xs: xs, meta, pointsOnly: true, fillPoints: true, psize: 12, pw: 2.5 });
      if (hot.some((y) => y != null)) markers.push({ label: "hot " + b, color: THROTC, ys: hot, _xs: xs, meta, pointsOnly: true, hollow: true, psize: 14, pw: 2.5 });
    });
    return [...markers, ...out];   // markers first -> drawn on top, win the hover tie
  };
  // off-scale ⊗ at a run whose SELECTED cell failed/OOM'd (never plotted on-curve as a low value)
  const failMk = (() => {
    const byX = {};
    branches.forEach((b) => runsFor(plat, b).forEach((r) => {
      const c = cellOf(r); if (!c || !c.failed) return;
      (byX[runTime(r)] = byX[runTime(r)] || { x: runTime(r), items: [] }).items.push({ b, c });
    }));
    return Object.values(byX).map((g) => ({ x: g.x,
      tip: `<b>failed config</b><br>` + g.items.map(({ b, c }) => `${b} — <span class="bad">${c.oom ? "OOM" : "FAILED"}</span>${c.error ? " · " + c.error : ""}`).join("<br>") }));
  })();
  const poTip = (spec, idx) => {
    const t = spec._xs[idx], m = spec.meta;
    const r = runsFor(m.platform, m.branch).find((x) => runTime(x) === t); if (!r) return null;
    const c = cellOf(r);
    let val = "";
    if (c && c.failed) val = `<br><span class="bad">${c.oom ? "OOM" : "FAILED"}</span>${c.error ? " — " + c.error : ""}`;
    else if (c) val = `<br><span class="tdim">time</span> ${c.min_ms != null ? fmtTime(c.min_ms / 60000) : "—"}`
      + `<br><span class="tdim">peak mem</span> ${fmtGB(c.mem_mb)}`
      + (c.speedup != null ? `<br><span class="tdim">speedup</span> ${c.speedup.toFixed(2)}×` : "")
      + (cellHot(c) ? `<br><span class="thr">⚠ ${hotWarn(c)}</span>` : "");
    return `<b>${m.branch}</b><br>${PLAT} · ${commitMinute(r)}`
      + `<br><span class="tdim">commit</span> ${r.commit}${r.dirty ? " · dirty" : ""}`
      + `<br><span class="tdim">${geom} · ${op} · ${shape} · n=${n}</span>${val}`;
  };
  const DAY = 86400;
  const xr = xs.length < 2 ? [xs[0] - DAY / 2, xs[0] + DAY / 2] : null;
  const xpad = xs.length > 1 ? (xs[xs.length - 1] - xs[0]) * 0.05 : 0;
  const poOnX = (min, max) => { poXWindow = (min != null && max != null && min <= xs[0] && max >= xs[xs.length - 1]) ? null : [min, max]; };
  const depMk = depChangeMarks(plat).concat(cfgChangeMarks(plat));
  const yr = (M.perop_yranges || {})[`${plat}|${geom}|${op}`] || {};   // fixed per-(platform, geom, op) y-axis (build-time)
  const poGroup = [];
  const base = { xTime: true, xRange: xr, xPadAdd: xpad, yLog: true, yfmt: fmtNum, onPick: pickRun,
    tooltip: poTip, syncX: poGroup, onXChange: poOnX, showNow: true, depMarks: depMk, failMarks: failMk, boxZoom: true };
  linePlot($("poTime"), xs, specsFor("time"), { width: $("poTime").clientWidth || 320, ...base, yLabelText: "min", yMinutes: true, yRange: yr.time });
  linePlot($("poMem"), xs, specsFor("mem"), { width: $("poMem").clientWidth || 320, ...base, yLabelText: "GB", yRange: yr.mem });
  if (poXWindow && $("poTime")._u) $("poTime")._u.setScale("x", { min: poXWindow[0], max: poXWindow[1] });

  // legend: one swatch per branch that actually has data (line 1), constant symbol keys (line 2) — the
  // two-row layout keeps the height stable so the panels don't jump when the branch set changes.
  const k = (c, t) => `<span class="k"><span class="sw" style="background:${c}"></span>${t}</span>`;
  const usedBranches = branches.filter((b) => Object.values(byBranch[b]).some((c) => c && !c.failed));
  const depLeg = depMk.length ? `<span class="k"><span style="display:inline-block;width:8px;height:8px;background:${DEPC};transform:rotate(45deg);margin:0 4px"></span>dep change (hover ◆)</span>` : "";
  const failLeg = failMk.length ? `<span class="k"><span class="cx" style="color:${FAILC}">⊗</span>failed config (hover)</span>` : "";
  $("po-legend").innerHTML =
    `<span class="hl-line"><span class="grp">${usedBranches.map((b) => k(branchColor(b), b)).join("") || '<span class="muted">no data for this selection</span>'}</span></span>` +
    `<span class="hl-line">` +
      `<span class="k"><span class="ring" style="border-color:${THROTC}"></span>ran hot</span>` +
      `<span class="k"><span class="dot" style="background:${THROTC}"></span>throttled</span>` +
      depLeg + failLeg +
      `<span class="k"><span class="ring" style="border-color:${CORRC}"></span>run shown</span>` +
    `</span>`;
}

// Device-count choices for the History `n` selector = the counts present in the ACTIVE group's
// geometries (cone/parallel have 1/2/4; translation/multiaxis are n=1 until sharding lands).  Clamps
// ui_state.histN into range so switching groups can't leave it on a count the new group lacks.
function syncHistN() {
  const group = HIST_GROUPS.find((g) => g.id === ui_state.histGroup) || HIST_GROUPS[0];
  const devs = uniq(M.runs.flatMap((r) => r.cells.filter((c) => group.geoms.includes(c.geom)).map((c) => c.ndev)))
    .filter((x) => x != null).sort((a, b) => a - b);
  if (!devs.length) devs.push(1);
  if (!devs.includes(ui_state.histN)) ui_state.histN = devs[0];
  fillSelect("histN", devs, ui_state.histN);
}

// ---- correctness banner + tab badge (the dashboard IS the alert — design note D5) ----------------
// The favicon doubles as a passive signal: a red "!" tile when any divergence is unacknowledged, so a
// pinned/bookmarked tab flags it without being opened.  SVG data-URI -> no asset, no infra.
function setFavicon(n) {
  let link = $("favicon");
  if (!link) { link = document.createElement("link"); link.id = "favicon"; link.rel = "icon"; document.head.appendChild(link); }
  const svg = n > 0
    ? `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="${CORRC}"/><text x="8" y="12.5" font-size="13" font-weight="bold" text-anchor="middle" fill="#fff" font-family="sans-serif">!</text></svg>`
    : `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="${PLATC.gpu}"/></svg>`;
  link.href = "data:image/svg+xml," + encodeURIComponent(svg);
}
let bannerCollapsed = false;                 // click the summary line to reduce the banner to just that line
const bannerOpen = new Set();                // runKeys whose full detail is expanded INLINE in the banner
function renderBanner() {
  const box = $("corr-banner"); if (!box) return;
  // the alert inbox: the LATEST run per (platform, branch) that is unacknowledged-incorrect — not every
  // historical run (those show as ✕ marks in History).  A branch auto-clears when its latest run is clean.
  const bad = [];
  M.platforms.forEach((p) => branchesFor(p).forEach((b) => { const r = latestRun(p, b); if (r && runAlert(r)) bad.push(r); }));
  bad.sort((a, b) => runTime(b) - runTime(a));
  setFavicon(bad.length);
  document.title = bad.length ? `⚠(${bad.length}) mbirtorch metrics` : "mbirtorch metrics";
  if (!bad.length) { box.style.display = "none"; box.innerHTML = ""; bannerOpen.clear(); return; }
  const badKeys = new Set(bad.map(runKey));
  [...bannerOpen].forEach((k) => { if (!badKeys.has(k)) bannerOpen.delete(k); });   // forget rows that cleared
  const since = bad.reduce((a, b) => (runTime(b) < runTime(a) ? b : a));
  // ONE compact line per (branch, platform) — no per-config bullets.  Clicking a line expands that run's
  // FULL detail inline (the same blocks as the main panel; click again to collapse); clicking the summary
  // head reduces the whole banner to just that line.  The banner is height-capped and scrolls internally
  // (CSS), so a long list can never cover the page and pin itself there.
  const row = (r) => {
    const rk = runKey(r), open = bannerOpen.has(rk), n = runCorrCells(r);
    return `<li class="cb-run">`
      + `<div class="cb-runhead" data-rk="${rk}"><span class="cb-caret">${open ? "▾" : "▸"}</span>`
      + ` <b>${r.branch}</b> · ${r.platform.toUpperCase()} · ${commitMinute(r)} · ${n} config${n > 1 ? "s" : ""}</div>`
      + (open ? `<div class="cb-detail">${corrDetailBlocks(r)}</div>` : "")
      + `</li>`;
  };
  box.style.display = "block";
  box.classList.toggle("collapsed", bannerCollapsed);
  box.innerHTML =
      `<div class="cb-head"><span class="cb-caret">${bannerCollapsed ? "▸" : "▾"}</span>`
    + ` ✕ ${bad.length} unacknowledged correctness divergence${bad.length > 1 ? "s" : ""} since ${runDateLabel(since)}</div>`
    + `<ul class="cb-list">${bad.map(row).join("")}</ul>`
    + `<div class="cb-foot">click a row for details (again to hide) · click the title to collapse · clear reviewed runs with <code>action_scripts/clear_correctness.sh</code></div>`;
  box.querySelector(".cb-head").onclick = () => { bannerCollapsed = !bannerCollapsed; renderBanner(); };
  box.querySelectorAll(".cb-runhead").forEach((h) => h.onclick = () => {
    const rk = h.dataset.rk;
    if (bannerOpen.has(rk)) bannerOpen.delete(rk); else bannerOpen.add(rk);
    renderBanner();   // self-contained: only the banner re-renders (inline), no jump to the main panel
  });
}

// ---- orchestration -----------------------------------------------------------
function renderAll() { renderBanner(); renderTiles(); renderDetail(); syncGoSelect(); renderScaling(); renderHistory(); renderPerOp(); }
// Default branch for a platform = the one with the MOST RECENT run (by commit time), NOT the
// alphabetically-first.  Keeps the run-shown tile on the newest run after a branch rename / new branch
// (e.g. greg/conebeam_sharding -> greg/sharding_extensions: the newest run is on the new branch).
function defaultBranch(plat) {
  const rs = M.runs.filter((r) => r.platform === plat);
  return rs.length ? rs.reduce((a, b) => (runTime(b) > runTime(a) ? b : a)).branch : (branchesFor(plat)[0] || null);
}
function onPlatform() {
  ui_state.platform = $("platform").value;
  const bs = branchesFor(ui_state.platform);
  if (!bs.includes(ui_state.branch)) ui_state.branch = defaultBranch(ui_state.platform);
  fillSelect("branch", bs, ui_state.branch);
  ui_state.openTile = null; ui_state.runKey = null; renderAll();
}
function init() {
  // The repo name at the end of the header line links to the repo (plain text if the URL is unknown).
  const repo = M.repo_url
    ? `<a class="repolink" href="${M.repo_url}" target="_blank" rel="noopener">${M.repo_name}</a>`
    : M.repo_name;
  $("gen").innerHTML = `generated ${M.generated} · ${repo}`;
  $("footer").innerHTML = `${M.runs.length} run(s) · platforms ${M.platforms.join(", ")} · branches ${M.branches.join(", ")} · regenerate with <code>action_scripts/build_dashboard.sh</code>`;
  if (!M.runs.length) { $("tiles").innerHTML = "<p class='muted'>No runs found under results/.</p>"; return; }

  // Open on the GLOBALLY most-recent run (by commit time), whatever platform/branch it's on — so a newer
  // CPU-only commit isn't hidden behind a stale GPU run (the old fixed "prefer gpu" default did exactly that).
  const newest = M.runs.reduce((a, b) => (runTime(b) > runTime(a) ? b : a));
  ui_state.platform = newest.platform;
  ui_state.branch = newest.branch;
  ui_state.runKey = runKey(newest);
  fillSelect("platform", M.platforms, ui_state.platform, platLabels());
  fillSelect("branch", branchesFor(ui_state.platform), ui_state.branch);

  $("platform").onchange = onPlatform;
  $("branch").onchange = () => { ui_state.branch = $("branch").value; ui_state.openTile = null; ui_state.runKey = null; renderAll(); };
  $("op").onchange = () => { ui_state.go = $("op").value; renderScaling(); };
  $("ref").value = ui_state.ref;
  $("ref").onchange = () => { ui_state.ref = $("ref").value; renderScaling(); };
  // History branch filter: "all" (default) overlays every branch; pick one to isolate it.
  fillSelect("histBranch", ["all", ...M.branches], ui_state.histBranch, ["all branches", ...M.branches]);
  $("histBranch").onchange = () => { ui_state.histBranch = $("histBranch").value; renderHistory(); };
  // History geometry-group toggle: swaps cone+parallel <-> translation+multiaxis (different headline op).
  $("hist-group-seg").innerHTML = HIST_GROUPS.map((g) =>
    `<button data-g="${g.id}" class="${g.id === ui_state.histGroup ? "on" : ""}">${g.label}</button>`).join("");
  $("hist-group-seg").querySelectorAll("button").forEach((b) => b.onclick = () => {
    ui_state.histGroup = b.dataset.g;
    $("hist-group-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    syncHistN(); renderHistory();
  });
  // History device-count selector (n): the device counts present in the ACTIVE group (the new
  // geometries only have n=1 until sharding lands, so the choices shrink when that group is selected).
  syncHistN();
  $("histN").onchange = () => { ui_state.histN = +$("histN").value; renderHistory(); };
  // Per-op history selectors (fully self-contained: its own platform/branch/op/shape/n).  Defaults to GPU
  // (the platform with the large sharded shapes); every dropdown is sticky — changing one keeps the rest.
  ui_state.poPlat = M.platforms.includes("gpu") ? "gpu" : ui_state.platform;
  fillSelect("poBranch", ["all", ...M.branches], ui_state.poBranch, ["all branches", ...M.branches]);
  syncPerOpSelectors();
  $("poPlat").onchange = () => { ui_state.poPlat = $("poPlat").value; renderPerOp(); };
  $("poBranch").onchange = () => { ui_state.poBranch = $("poBranch").value; renderPerOp(); };
  $("poOp").onchange = () => { ui_state.poGo = $("poOp").value; renderPerOp(); };
  $("poShape").onchange = () => { ui_state.poShape = $("poShape").value; renderPerOp(); };
  $("poN").onchange = () => { ui_state.poN = +$("poN").value; renderPerOp(); };
  $("view-seg").innerHTML = `<button data-v="plot" class="on">plot</button><button data-v="table">table</button>`;
  $("view-seg").querySelectorAll("button").forEach((b) => b.onclick = () => {
    ui_state.view = b.dataset.v; $("view-seg").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); renderScaling();
  });
  let rt; window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { renderScaling(); renderHistory(); renderPerOp(); }, 160); });
  renderAll();
}
init();
