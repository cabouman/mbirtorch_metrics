#!/usr/bin/env python3
"""Render a regression_*.yaml as a browsable *_table.yaml, nested geometry -> op -> size -> n_devices.

The dashboard is the rich view; this is the quick, diff-able, per-run dump.  It is itself YAML so
PyCharm's structure breadcrumb accumulates the `GEOMETRY .. / OP .. / size .. / n=..` path as you scroll
(and so it parses cleanly).  The `run:` block carries the run's identity -- commit + toolchain, to pin a
perf shift to code rather than the torch and CUDA stack.  Each size opens with a `labels:` header naming the columns,
then one quoted, column-aligned row per device count -- `<t> ms   <m> MB   <s>x` (+ `notes` when the
cell regressed), lined up under the header with the (fixed-decimal, thousands-separated) numbers
decimal-aligned file-wide.  `notes` describes any gate regression, not config noise like trial counts.

The headings fold the label into the key (`GEOMETRY cone:`, not `GEOMETRY: cone`) because a YAML key
can't carry both a scalar value and child keys -- this keeps the breadcrumb readable and the file valid.

Usage:
    python regression_to_table.py <regression.yaml> [output.yaml]

With no output path, writes alongside the input as <basename>_table.yaml.
"""
import os
import re
import sys

# Display order; anything not listed falls to the end, alphabetically.
GEOM_ORDER = ["parallel", "cone", "translation", "multiaxis_parallel", "denoiser"]
OP_ORDER = ["direct_filter", "forward", "back", "vcd_nonconst", "denoise"]


def load_yaml(path):
    from ruamel.yaml import YAML
    return YAML(typ="safe").load(open(path))


def size_vol(s):
    try:
        v = 1
        for x in str(s).split("x"):
            v *= int(x)
        return v
    except Exception:
        return 0


def order_key(value, order):
    return (order.index(value) if value in order else len(order), str(value))


def _fmt_num(v):
    """A measurement number formatted like the old text dump: thousands separators, one decimal."""
    return f"{v:,.1f}" if isinstance(v, (int, float)) else "—"


def _fmt_spd(v):
    return f"{v:.2f}x" if isinstance(v, (int, float)) else "—"


# ---- gate notes: regressions vs the prior run, attached to the cell they name ----
def _gate_entry_parts(entry):
    """A gate list entry -> (cell-coord 'geom|op|size|ndev', description).  Entries are pre-formatted
    strings like '[prior:...] cone|back|512x448x384|1 memory X vs Y (+31.2%)'; dict form is tolerated."""
    if isinstance(entry, dict):
        return entry.get("cell"), entry.get("text", "")
    s = re.sub(r"^\s*\[[^\]]*\]\s*", "", str(entry))   # drop the leading [prior:...] tag
    m = re.match(r"(\S+)\s+(.*)", s)
    return (m.group(1), m.group(2)) if m else (None, str(entry))


def _gate_desc(desc):
    """Condense a gate description to e.g. 'memory +31.2%', 'time +117.6%', or 'now fails (OOM)'."""
    word = (re.match(r"(\w+)", desc) or [None, ""])[1]
    if word.upper() == "REGRESSED":
        oom = "RESOURCE_EXHAUSTED" in desc or "OOM" in desc.upper()
        return "now fails (OOM)" if oom else "now fails"
    pct = re.search(r"([+-][\d.]+)%", desc)
    return f"{word} {pct.group(1)}%" if (pct and word) else desc[:48]


def build_gate_index(doc):
    """(geom, op, size, ndev) -> ['HARD: memory +31.2%', 'soft: time +117.6%', ...] from the gate."""
    idx = {}
    g = doc.get("gate") or {}
    for sev, key in (("HARD", "hard"), ("soft", "soft")):
        for entry in (g.get(key) or []):
            coord, desc = _gate_entry_parts(entry)
            parts = (coord or "").split("|")
            if len(parts) != 4:
                continue
            try:
                k = (parts[0], parts[1], parts[2], int(parts[3]))
            except ValueError:
                continue
            idx.setdefault(k, []).append(f"{sev}: {_gate_desc(desc)}")
    return idx


def cell_notes(c, gate_notes):
    """Per-cell notes: failures/OOM (from the cell) + this cell's gate regressions; throttling if the
    timing is suspect.  Deliberately NOT trial counts -- notes are about regressions/gates, not config."""
    notes = []
    if c.get("failed") or c.get("oom"):
        notes.append("OOM" if c.get("oom") else "FAILED")
        if c.get("error"):
            notes.append(str(c["error"])[:60])
    if c.get("throttled"):
        notes.append("throttled")
    notes.extend(gate_notes)
    return "; ".join(notes)


# ---- build the nested table ----
def run_block(doc, CM):
    m = CM()
    m["kind"] = doc.get("kind")
    m["platform"] = doc.get("platform")
    m["branch"] = doc.get("git_branch")
    m["commit"] = (doc.get("git_commit") or "")[:8] or None
    m["commit_date"] = doc.get("git_commit_date")
    m["mbirtorch_version"] = doc.get("mbirtorch_version")
    m["date"] = doc.get("date")
    m["device"] = doc.get("device_label")
    tc = doc.get("toolchain")
    m["toolchain"] = dict(tc) if isinstance(tc, dict) else "(not recorded in this run)"
    sbg = doc.get("sharding_by_geom")
    if sbg:
        m["sharding_by_geom"] = dict(sbg)
    m["device_counts"] = doc.get("device_counts")
    g = doc.get("gate") or {}
    gate = CM()
    gate["result"] = g.get("result")
    gate["hard"] = len(g.get("hard") or [])
    gate["soft"] = len(g.get("soft") or [])
    m["gate"] = gate
    return m


def to_table(doc):
    from ruamel.yaml.comments import CommentedMap as CM
    from ruamel.yaml.scalarstring import SingleQuotedScalarString as Q
    gate_idx = build_gate_index(doc)
    cells = doc.get("cells") or []
    # group: geom -> op -> size -> {ndev: cell}
    tree = {}
    for c in cells:
        tree.setdefault(c.get("geometry"), {}) \
            .setdefault(c.get("op"), {}) \
            .setdefault(c.get("size"), {})[c.get("n_devices")] = c

    # Field widths over EVERY cell, so the labels line up and the (fixed-decimal) numbers are
    # decimal-point aligned file-wide, not just within one size group.
    w_min = max((len(_fmt_num(c.get("min_ms"))) for c in cells), default=1)
    w_mem = max((len(_fmt_num(c.get("mem_mb"))) for c in cells), default=1)
    w_spd = max((len(_fmt_spd(c.get("speedup"))) for c in cells), default=5)
    # Column widths so each size's `labels:` header and the device rows below it line up.  A column is
    # as wide as the wider of its label and its values; '+3' leaves room for the ' ms'/' MB' unit.  The
    # time/memory columns are RIGHT-justified (header + value) so 'min time'/'peak mem' end where the
    # 'ms'/'MB' do; speedup/notes are left-justified.
    ctw, cmw, csw = max(len("min time"), w_min + 3), max(len("peak mem"), w_mem + 3), max(len("speedup"), w_spd)
    header = f"{'min time':>{ctw}}   {'peak mem':>{cmw}}   {'speedup':<{csw}}   notes"

    def row_for(c):
        t = f"{_fmt_num(c.get('min_ms')):>{w_min}} ms"
        m = f"{_fmt_num(c.get('mem_mb')):>{w_mem}} MB"
        s = f"{_fmt_spd(c.get('speedup')):>{w_spd}}"
        return f"{t:>{ctw}}   {m:>{cmw}}   {s:<{csw}}"

    out = CM()
    out["run"] = run_block(doc, CM)
    for geom in sorted(tree, key=lambda g: order_key(g, GEOM_ORDER)):
        gmap = CM()
        for op in sorted(tree[geom], key=lambda o: order_key(o, OP_ORDER)):
            omap = CM()
            for size in sorted(tree[geom][op], key=size_vol):
                smap = CM()
                bynd = tree[geom][op][size]
                nds = sorted(bynd, key=lambda x: (x is None, x))
                # The keys differ in width ('labels' vs 'n=1'), which would offset the row CONTENT by
                # that difference -- so left-pad each value to a common content column.  Every row is
                # force-QUOTED so the pad and any ':' in notes survive and the leading column is
                # identical (mixed quoting would shift only some rows and break the alignment).
                klen = max([len("labels")] + [len(f"n={nd}") for nd in nds])
                pad = lambda key, text: Q(" " * (klen - len(key)) + text.rstrip())
                smap["labels"] = pad("labels", header)
                for nd in nds:
                    c = bynd[nd]
                    notes = cell_notes(c, gate_idx.get((geom, op, size, nd), []))
                    row = row_for(c) + (f"   {notes}" if notes else "")
                    smap[f"n={nd}"] = pad(f"n={nd}", row)
                omap[f"size {size}"] = smap
            gmap[f"OP {op}"] = omap
        out[f"GEOMETRY {geom}"] = gmap
    return out


def write_table(doc, out_path):
    from ruamel.yaml import YAML
    y = YAML()
    y.default_flow_style = False
    y.width = 1 << 30          # never wrap a long notes line
    with open(out_path, "w") as f:
        y.dump(to_table(doc), f)


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    in_path = argv[1]
    out_path = argv[2] if len(argv) > 2 else os.path.splitext(in_path)[0] + "_table.yaml"
    write_table(load_yaml(in_path), out_path)
    sys.stderr.write(f"[wrote {out_path}]\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
