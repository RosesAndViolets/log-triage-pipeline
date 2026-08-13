"""Turn one recorded run into a standalone replay page.

    python export_run.py --list
    python export_run.py --run 20260813T163908 --out map.html

Reads runs.db and logs.db, inlines the run into map_template.html, and writes a
single self-contained file — no server, no network, openable anywhere. The page
states whether the chain verified, so a replay that does not match the run it
claims to show says so on its face instead of looking identical to one that does.
"""

import json
import sys
from pathlib import Path

from triagelab.core import store

ROOT = Path(__file__).parent
TEMPLATE = ROOT / "map_template.html"
MARKER = "/*__RUN_DATA__*/ null"


def build(run_id: str) -> dict:
    """Shape one run for the page. Everything here comes out of the databases."""
    d = store.load_run(run_id)
    run, events = d["run"], d["events"]

    truth = _truth_from(events, d["logs"])
    results, verdict_seq = _results_from(events)

    return {
        "run_id": run["run_id"],
        "mode": run["mode"],
        "model": run["model"],
        "status": run["status"],
        "started": run["started"],
        "condition": run["condition"],
        "intact": d["intact"],
        "events": events,
        "truth": truth,
        "results": results,
        "verdict_seq": verdict_seq,
    }


def _truth_from(events: list[dict], logs: list[dict]) -> dict:
    """Ground truth is only known where the run recorded a grade against it.

    A run with no grading gets an honest placeholder rather than an invented
    one — the page must never show a cause nobody established.
    """
    graded = [e for e in events if e["kind"] == "grade"]
    err = next((l for l in logs if (l.get("level") or "") in ("ERROR", "FATAL")), None)
    loc = ""
    if err and err.get("source"):
        loc = (err["source"].splitlines() or [""])[0]
    if not graded:
        return {
            "loc": loc or "—",
            "was": "", "now": "",
            "cause": "This run recorded no grade, so no ground truth was compared. "
                     "The replay below is still the real chain.",
            "hid": "",
        }
    return {
        "loc": loc or "—",
        "was": "", "now": "",
        "cause": graded[0]["payload"].get("reasoning", ""),
        "hid": "Ground truth lives in TRUTH[fingerprint] and reaches nothing "
               "before the judge.",
    }


def _results_from(events: list[dict]):
    """The comparison table, built from the verdict and grade events."""
    verdict = next((e for e in events if e["kind"] == "verdict"), None)
    grade = next((e for e in events if e["kind"] == "grade"), None)
    if not verdict:
        return [], 0
    vp, seq = verdict["payload"], verdict["seq"]
    rows = []
    if grade:
        gp, gseq = grade["payload"], grade["seq"]
        rows += [
            {"axis": "Root cause", "got": vp.get("root_cause", ""),
             "truth": gp.get("reasoning", ""),
             "v": "ok" if gp.get("cause_correct") else "bad",
             "sc": f"{gp.get('cause_score', 0):.2f}", "at": gseq},
            {"axis": "Proposed fix", "got": vp.get("proposed_fix", ""),
             "truth": "Judged on whether it removes the defect, not whether it sounds right",
             "v": "ok" if gp.get("fix_correct") else "bad",
             "sc": f"{gp.get('fix_score', 0):.2f}", "at": gseq},
        ]
    else:
        rows += [
            {"axis": "Root cause", "got": vp.get("root_cause", ""),
             "truth": "not graded in this run", "v": "mut", "sc": "—", "at": seq},
            {"axis": "Proposed fix", "got": vp.get("proposed_fix", ""),
             "truth": "not graded in this run", "v": "mut", "sc": "—", "at": seq},
        ]
    rows.append({
        "axis": "Confidence",
        "got": f"{vp.get('confidence', 0):.2f} — "
               + ("escalated to a human" if vp.get("needs_human_review")
                  else "cleared 0.8, routed without waking anyone"),
        "truth": "—", "v": "mut", "sc": f"{vp.get('confidence', 0):.2f}", "at": seq})
    return rows, seq


def export(run_id: str, out: Path) -> Path:
    if not TEMPLATE.exists():
        raise SystemExit(f"missing template: {TEMPLATE}")
    html = TEMPLATE.read_text(encoding="utf-8")
    if MARKER not in html:
        raise SystemExit(f"template has no {MARKER} marker — was it already exported?")
    data = build(run_id)
    # ensure_ascii keeps the payload safe regardless of the page's charset, and
    # </script> inside a string would otherwise close the block early.
    blob = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    out.write_text(html.replace(MARKER, blob), encoding="utf-8")
    return out


def main(argv: list[str]):
    store.use_utf8_stdout()

    if "--list" in argv or not argv:
        runs = store.list_runs()
        if not runs:
            print("no runs recorded yet — run mockapp.py or realapp.py first")
            return
        print(f"{'run_id':<18} {'mode':<9} {'events':>7}  {'chain':<8} status")
        for r in runs:
            ok = "ok" if store.verify(r["run_id"]) is None else "BROKEN"
            print(f"{r['run_id']:<18} {r['mode']:<9} {r['events']:>7}  {ok:<8} {r['status']}")
        return

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    run_id = opt("--run") or store.latest_run_id()
    if not run_id:
        raise SystemExit("no run to export — run mockapp.py or realapp.py first")
    out = Path(opt("--out", "map.html"))

    broken_at = store.verify(run_id)
    path = export(run_id, out)
    d = store.load_run(run_id)
    print(f"exported run {run_id} → {path}")
    print(f"  {len(d['events'])} events, {len(d['logs'])} log records")
    if broken_at is None:
        print("  chain verified — the replay matches the run")
    else:
        print(f"  ⚠️  CHAIN BROKEN at seq {broken_at} — the page says so too")


def _self_check():
    import shutil
    import tempfile

    real = (store.LOGS_DB, store.RUNS_DB)
    tmp = Path(tempfile.mkdtemp())
    store.LOGS_DB, store.RUNS_DB = tmp / "l.db", tmp / "r.db"
    try:
        with store.open_run(model="m", mode="agentic", run_id="x1") as (rid, ch):
            store.write_log(rid, {"serviceName": "svc", "level": "ERROR", "message": "boom",
                                  "trace_id": "t", "error": {"class": "KeyError",
                                                             "source": "constructor.py:238 in f"}})
            ch.append("ingest.gate", "exit", {"service": "svc", "level": "ERROR"})
            ch.append("triage.investigate", "tool", {"name": "read_source", "args": {"path": "p"}})
            ch.append("triage.verdict", "verdict",
                      {"root_cause": "rc", "proposed_fix": "pf", "confidence": 0.92,
                       "needs_human_review": False})
            ch.append("triage.judge", "grade",
                      {"cause_correct": True, "cause_score": 1.0, "fix_correct": False,
                       "fix_score": 0.2, "reasoning": "cause right, fix works around it"})

        d = build("x1")
        assert d["intact"] and d["mode"] == "agentic"
        assert d["truth"]["cause"].startswith("cause right")
        axes = {r["axis"]: r for r in d["results"]}
        assert axes["Root cause"]["v"] == "ok" and axes["Root cause"]["sc"] == "1.00"
        # the axis that used to be invisible: a correct cause with a wrong fix
        assert axes["Proposed fix"]["v"] == "bad" and axes["Proposed fix"]["sc"] == "0.20"
        assert axes["Confidence"]["sc"] == "0.92"

        out = export("x1", tmp / "m.html")
        html = out.read_text(encoding="utf-8")
        assert MARKER not in html, "marker survived — the run was not inlined"

        # --- the template's own invariants -------------------------------
        # A duplicate top-level `const` is a SyntaxError that blanks the whole
        # page, and no Python check would see it. This caught a real collision:
        # the translator was briefly named L, which the LAYOUT accessor owns.
        import collections
        import re as _re
        script = html.split("<script>")[-1]
        names = _re.findall(r"^const ([A-Za-z_$][\w$]*)\s*=", script, _re.M)
        dupes = [n for n, c in collections.Counter(names).items() if c > 1]
        assert not dupes, f"duplicate top-level const in map_template.html: {dupes}"

        # The language layer must fall back, never blank: English is the
        # default, and every translation value has to be a non-empty string.
        assert 'const LANG' in script and 'const TXT' in script
        for tbl, least in (("JA", 40), ("JA_SUB", 5)):
            body = script.split(f"const {tbl} = {{", 1)[1].split("\n};", 1)[0]
            vals = _re.findall(r':\s*"((?:[^"\\]|\\.)*)"', body)
            assert len(vals) >= least, f"{tbl} looks empty: {len(vals)} entries"
            assert all(v.strip() for v in vals), f"{tbl} has a blank translation"
        # Japanese must actually be present, and English must remain the default.
        assert "日本語" in script and 'localStorage.getItem("triage-lang") || "en"' in script
        assert '"run_id": "x1"' in html or '"run_id":"x1"' in html
        assert "</script>" not in html.split("RUN = ")[1][:4000], \
            "payload could close the script block early"

        # a run with no grading must not invent ground truth
        with store.open_run(model="m", mode="push", run_id="x2") as (_, ch2):
            ch2.append("ingest.gate", "exit", {"level": "INFO", "halted": True})
        d2 = build("x2")
        assert d2["results"] == [] and "no grade" in d2["truth"]["cause"]

        # a tampered chain must be reported, not silently rendered
        with store.runs_db() as c:
            c.execute("UPDATE event SET payload='{}' WHERE run_id='x1' AND seq=1")
        assert build("x1")["intact"] is False
        # the page must carry the verdict on its own provenance, not just the
        # template's boilerplate string for it
        blob = export("x1", tmp / "m2.html").read_text(encoding="utf-8")
        blob = blob.split("RUN = ")[1][:6000]
        assert '"intact": false' in blob or '"intact":false' in blob, \
            "a broken chain was inlined as if it were intact"

        print("export self-check ok — run inlined, cause/fix split survives, "
              "ungraded run stays honest, broken chain is surfaced")
    finally:
        store.LOGS_DB, store.RUNS_DB = real
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        store.use_utf8_stdout()
        _self_check()
    else:
        main(sys.argv[1:])
