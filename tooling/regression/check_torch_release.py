#!/usr/bin/env python3
"""Warn, in the nightly log and email, when a torch NEWER than the last-reviewed version has
shipped on PyPI.

Why this exists: mbirtorch declares `torch>=2.13` with no upper bound, so any torch release can
change measured performance without any change to mbirtorch.  A framework release has done exactly
that before, in jax 0.10.2, which ran GPU forward projection three to nine times slower.  This
script surfaces "a new torch is out" so it gets re-tested with
tooling/scaling_tests/measure_one_cell.py, and so the dependency canary can re-measure a fixed
commit under it.

Workflow on an alert:
  - the new torch is good  -> bump TORCH_LAST_REVIEWED in action_scripts/run_configs.env,
  - the new torch is bad   -> pin torch in mbirtorch's pyproject AND bump TORCH_LAST_REVIEWED.
TORCH_LAST_REVIEWED is the highest torch version that has been ASSESSED, good or bad, so a version
that was assessed and rejected still belongs there; the alert then fires only for versions past it.

Usage:
    check_torch_release.py <last-reviewed-version>    e.g.  check_torch_release.py 2.13.0
    check_torch_release.py --print-latest             the PyPI-latest version, for the canary
    check_torch_release.py --headroom <env-python>    can that version install on this env?

Best-effort and NON-FATAL: any error, including no network, exits 0 silently, so it never disturbs
the nightly.
"""
import json
import sys
import urllib.request

PYPI = "https://pypi.org/pypi/torch/json"


def _is_newer(latest, reviewed):
    try:
        from packaging.version import parse
        return parse(latest) > parse(reviewed)
    except Exception:
        import re
        tup = lambda v: tuple(int(x) for x in re.findall(r"\d+", v))   # noqa: E731
        try:
            return tup(latest) > tup(reviewed)
        except Exception:
            return latest != reviewed   # last resort: any difference


def _pypi_latest():
    try:
        with urllib.request.urlopen(PYPI, timeout=15) as r:
            return json.load(r)["info"]["version"]
    except Exception:
        return None   # offline or proxy hiccup


def _pypi_requires_python(version):
    """The ``requires_python`` metadata of one torch release (e.g. ">=3.10"); "" on any error."""
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/torch/{version}/json", timeout=15) as r:
            return json.load(r)["info"].get("requires_python") or ""
    except Exception:
        return ""


def _ver_tuple(v):
    import re
    m = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in m[:2]) if len(m) >= 2 else (tuple(int(x) for x in m[:1]) if m else None)


def _min_python(requires_python):
    """Lower bound of a ``requires_python`` spec (">=3.10" or ">=3.10,<3.14") -> (3, 10); else None."""
    import re
    m = re.search(r">=\s*(\d+)\.(\d+)", requires_python or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def main(argv):
    # `--print-latest`: emit just the PyPI-latest torch version, and nothing on failure.  The
    # dependency canary compares it with state/<plat>/torch_seen.
    if len(argv) > 1 and argv[1] == "--print-latest":
        v = _pypi_latest()
        if v:
            print(v)
        return 0
    # `--headroom <env-python> [available]`: say whether the latest available torch can actually
    # install on the regression env's Python.  This surfaces the silent case where a newer torch
    # shipped but the env Python floor holds the install to an older one.  `available` defaults to
    # the PyPI latest.  Prints one human line, or nothing on error, and always exits 0.
    if len(argv) > 1 and argv[1] == "--headroom":
        env_py = (argv[2].strip() if len(argv) > 2 else "")
        avail = (argv[3].strip() if len(argv) > 3 else "") or _pypi_latest()
        if not avail:
            return 0
        rp = _pypi_requires_python(avail)
        floor, envt = _min_python(rp), _ver_tuple(env_py)
        if floor and envt and envt < floor:
            print(f"[torch-headroom] torch {avail} is available but requires Python {rp or '>=?'}; the "
                  f"regression env is Python {env_py} -> HELD BACK (pip resolves to the newest torch "
                  f"that supports Python {env_py}).  To adopt {avail}, bump CONDA_PYTHON in "
                  f"run_configs.env and recreate the regression conda env.")
        else:
            print(f"[torch-headroom] torch {avail} is available and Python-compatible with the env "
                  f"(env Python {env_py or '?'}).  If the install still resolves to an older torch, "
                  f"that is a pin in mbirtorch's pyproject, not the Python floor.")
        return 0
    reviewed = (argv[1].strip() if len(argv) > 1 else "")
    if not reviewed:
        return 0
    latest = _pypi_latest()
    if latest is None:
        return 0   # offline or proxy hiccup: stay silent, never fail the nightly
    if _is_newer(latest, reviewed):
        print(f"[torch-watch] NEW torch on PyPI: {latest}  (last reviewed: {reviewed}).  Re-test it "
              f"with tooling/scaling_tests/measure_one_cell.py.  If it is good, bump "
              f"TORCH_LAST_REVIEWED in run_configs.env.  If it regresses, pin torch in mbirtorch's "
              f"pyproject and bump TORCH_LAST_REVIEWED anyway.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
