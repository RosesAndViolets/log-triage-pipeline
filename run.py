#!/usr/bin/env python3
"""The one command you invoke. Everything else is a subcommand of this.

    python run.py check                          verify the wiring (no API key)
    python run.py mock   [--agentic] [--pick N] [--disable NODE,NODE]
    python run.py real   [--agentic] [--logged Y --injected X --seed N] [--pick N]
    python run.py runs                           what has been recorded
    python run.py export [--run ID] [--out FILE] one run -> a standalone page
    python run.py serve  [--port 8000]           drive the pipeline from a browser

Run it from anywhere: the repo root is put on sys.path here, so no subcommand
needs a path shim of its own.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from triagelab.core import store  # noqa: E402  (after the path is set)

USAGE = __doc__


def cmd_check(argv):
    """Every self-check plus the portability guard, in one command."""
    # Call _self_check() directly rather than running each module as __main__:
    # triage.py's __main__ is a live demo that would spend real requests, and a
    # verification command must never cost quota.
    mods = [
        "triagelab.core.store",
        "triagelab.core.dag",
        "triagelab.core.nodes",
        "triagelab.toolserver.tools",
        "triagelab.core.triage",
        "triagelab.viewer.export_run",
        "triagelab.harness.mockapp",
        "triagelab.harness.realapp",
    ]
    failed = []
    for mod in mods:
        r = subprocess.run(
            [sys.executable, "-c", f"import {mod} as m; m._self_check()"],
            cwd=ROOT, capture_output=True, text=True)
        ok = r.returncode == 0
        line = (r.stdout.strip().splitlines() or [""])[-1] if ok else \
            (r.stderr.strip().splitlines() or [""])[-1]
        print(f"  {'ok  ' if ok else 'FAIL'}  {mod:<34} {line[:88]}")
        if not ok:
            failed.append((mod, r.stderr or r.stdout))

    r = subprocess.run([sys.executable, "scripts/check_portable.py"],
                       cwd=ROOT, capture_output=True, text=True)
    print(f"  {'ok  ' if r.returncode == 0 else 'FAIL'}  {'portability':<34} "
          f"{r.stdout.strip().splitlines()[-1][:90] if r.stdout.strip() else ''}")
    if r.returncode != 0:
        print(r.stdout)
        failed.append(("portability", r.stdout))

    if failed:
        print(f"\n{len(failed)} check(s) failed:\n")
        for mod, err in failed:
            print(f"--- {mod} ---\n{err.strip()[-1200:]}\n")
        return 1
    print("\nall checks passed — no API key was needed for any of them")
    return 0


def cmd_mock(argv):
    from triagelab.harness import mockapp
    mockapp.main(argv)
    return 0


def cmd_real(argv):
    from triagelab.harness import realapp
    realapp.main(argv)
    return 0


def cmd_runs(argv):
    runs = store.list_runs()
    if not runs:
        print("no runs yet — try:  python run.py mock --pick 1")
        return 0
    print(f"{'run_id':<18} {'harness':<8} {'mode':<9} {'events':>7}  {'chain':<8} status")
    for r in runs:
        cond = r.get("condition") or "{}"
        harness = "real" if '"real"' in cond else "mock"
        ok = "ok" if store.verify(r["run_id"]) is None else "BROKEN"
        print(f"{r['run_id']:<18} {harness:<8} {r['mode']:<9} {r['events']:>7}  "
              f"{ok:<8} {r['status']}")
    return 0


def cmd_export(argv):
    from triagelab.viewer import export_run
    export_run.main(argv)
    return 0


def cmd_serve(argv):
    from triagelab.viewer import serve
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 8000
    serve.main(port)
    return 0


COMMANDS = {
    "check": cmd_check, "mock": cmd_mock, "real": cmd_real,
    "runs": cmd_runs, "export": cmd_export, "serve": cmd_serve,
}


def main():
    store.use_utf8_stdout()
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd = argv[0]
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}\n")
        print(USAGE)
        return 2
    return COMMANDS[cmd](argv[1:]) or 0


if __name__ == "__main__":
    sys.exit(main())
