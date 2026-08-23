#!/usr/bin/env python
"""Guided clear of reviewed correctness divergences (correctness-gating design note D6).

Writes ``results/correctness_acks.yaml`` with a single ``cleared_through: <date>`` watermark: every
correctness divergence on a commit dated <= that date is then acknowledged — greyed on the dashboard
and dropped from the banner / browser-tab badge.  Defaults to clearing through TODAY.  Reuses
``build_dashboard.collect_data()`` so it shows exactly the divergences the dashboard sees.

  action_scripts/clear_correctness.sh              # the one-liner: print status, then confirm [Y/n]
  action_scripts/clear_correctness.sh 2026-06-20   # clear through an explicit earlier date
  action_scripts/clear_correctness.sh --status     # print status only, never prompt or write
"""
import sys
import datetime
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_dashboard as bd  # noqa: E402  (path set above)


def _run_date(r):
    """A run's date as YYYY-MM-DD (commit date preferred, else the collection date), or None."""
    cd = r.get("commit_date")
    if cd:
        return cd[:10]
    d = r.get("date") or ""
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if (len(d) == 8 and d.isdigit()) else None


def _latest_per_branch(runs):
    def t(r):
        return r.get("commit_date") or r.get("date") or ""
    latest = {}
    for r in runs:
        k = (r["platform"], r["branch"])
        if k not in latest or t(r) > t(latest[k]):
            latest[k] = r
    return list(latest.values())


def _acked(r, through):
    d = _run_date(r)
    return through is not None and d is not None and d <= through


def main(argv):
    status_only = "--status" in argv
    assume_yes = ("--yes" in argv) or ("-y" in argv)
    dates = [a for a in argv if not a.startswith("-")]
    target = dates[0] if dates else datetime.date.today().isoformat()
    try:
        datetime.date.fromisoformat(target)
    except ValueError:
        print(f"bad date {target!r} — use YYYY-MM-DD", file=sys.stderr)
        return 2

    data = bd.collect_data()
    current = data.get("cleared_through")
    acks_path = bd.REPO_ROOT / "results" / "correctness_acks.yaml"

    # The alert inbox mirrors the dashboard banner: latest run per branch that is INCORRECT and not yet
    # acknowledged.  "would_clear" are those a watermark at <target> would sweep up (commit date <= target).
    incorrect = [r for r in _latest_per_branch(data["runs"]) if r.get("correctness")]
    active = [r for r in incorrect if not _acked(r, current)]
    would_clear = [r for r in active if (_run_date(r) or "9999-99-99") <= target]
    remain = [r for r in active if r not in would_clear]

    print(f"\ncurrent watermark : cleared_through = {current or '(none)'}")
    print(f"clearing through  : {target}\n")
    if not active:
        print("No unacknowledged correctness divergences — nothing to clear.")
        return 0

    def _list(runs):
        for r in sorted(runs, key=lambda r: _run_date(r) or ""):
            print(f"\n  {r['platform']}/{r['branch']} @ {r['commit']} ({_run_date(r)})")
            for c in sorted({f["cell"] for f in r["correctness"]}):
                print(f"      {bd._fmt_cell(c)}")

    print(f"{len(would_clear)} run(s) WOULD be cleared (commit date <= {target}):")
    _list(would_clear)
    if remain:
        print(f"\n{len(remain)} run(s) would REMAIN flagged (commit date > {target}):")
        _list(remain)

    if status_only:
        return 0
    if not would_clear:
        print("\nNothing on or before that date to clear — nothing written.")
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            print("\n(not a tty — re-run with --yes to write non-interactively); nothing written.")
            return 0
        if input(f"\nClear through {target}? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            print("Aborted — nothing written.")
            return 0

    acks_path.parent.mkdir(parents=True, exist_ok=True)
    acks_path.write_text(
        "# Correctness 'reviewed-through' watermark (correctness-gating design note D6).\n"
        "# Every correctness divergence on a commit dated <= this is acknowledged: greyed on the\n"
        "# dashboard, dropped from the banner / tab badge.  Set by action_scripts/clear_correctness.sh.\n"
        f"cleared_through: {target}\n")
    print(f"\nWrote {acks_path}  (cleared_through: {target}).")
    print("Next: rebuild the dashboard (action_scripts/build_dashboard.sh) and commit the acks file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
