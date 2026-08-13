"""The pipeline, declared as a graph.

Node bodies call the functions that already exist in `triage.py` — this is a
change of orchestration, not of logic. `normalize()`, `fingerprint()`,
`_fetch_context()`, `_agentic_call()` and `alert()` are unchanged and still
work when called directly.

Two graphs, because the pipeline genuinely has two rhythms: ingestion runs once
per log record, triage runs once per signature somebody chose to spend a
request on. Both write onto the same chain, so the record reads as one story.
"""

import json

from triagelab.core import triage
from triagelab.core.dag import DAG, HALT, Node

# --- ingestion: one pass per log record ------------------------------------


def _gate(ctx: dict):
    """Level filter. The cheapest check runs first, and stops the rest.

    An INFO line is not an error to be triaged, so the walk ends here — the
    record still lives in the buffer and the log store as context.
    """
    r = ctx["record"]
    lvl = (r.get("level") or "").upper()
    # Written into the context either way: a replay has to be able to say which
    # record was turned away, not just that something was.
    ctx.update({"service": r.get("serviceName"), "trace_id": r.get("trace_id"),
                "message": (r.get("message") or "")[:200], "level": lvl})
    if lvl not in ("ERROR", "FATAL"):
        return HALT
    return {"passed": True}


def _normalize(ctx: dict) -> dict:
    raw = ctx["record"].get("message", "")
    return {"raw_message": raw, "normalized": triage.normalize(raw)}


def _fingerprint(ctx: dict) -> dict:
    r = ctx["record"]
    key = f"{r['serviceName']}|{r['error']['class']}|{ctx['normalized']}"
    return {"key": key, "fingerprint": triage.fingerprint(r)}


def _count(ctx: dict) -> dict:
    p, fp = ctx["pipeline"], ctx["fingerprint"]
    p.counts[fp] += 1
    p.samples.setdefault(fp, ctx["record"])
    n = p.counts[fp]
    return {"count": n, "hot": n >= p.threshold, "threshold": p.threshold}


# ponytail: two events per record. Fine at prototype volume; a million records
# a day wants sampling, or gate decisions aggregated per batch.
INGEST = DAG((
    Node("ingest.gate", fn=_gate,
         records=("service", "trace_id", "message", "level", "passed")),
    Node("ingest.normalize", needs=("ingest.gate",), fn=_normalize,
         records=("raw_message", "normalized")),
    Node("ingest.fingerprint", needs=("ingest.normalize",), fn=_fingerprint,
         records=("key", "fingerprint")),
    Node("ingest.count", needs=("ingest.fingerprint",), fn=_count,
         records=("fingerprint", "count", "hot", "threshold")),
))


# --- triage: one pass per chosen signature ---------------------------------


def _select(ctx: dict) -> dict:
    """What the operator picked, and what it cost to look at it."""
    p, fp = ctx["pipeline"], ctx["fingerprint"]
    log = p.samples[fp]
    return {"record": log, "count": p.counts[fp],
            "service": log["serviceName"], "error_class": log["error"]["class"],
            "model": triage.MODEL}


def _context(ctx: dict) -> dict:
    """Push mode: context is chosen for the model before the call."""
    packet = ctx["pipeline"]._fetch_context(ctx["record"])
    return {"context_chars": len(packet), "context": packet[:2000]}


def _investigate(ctx: dict) -> dict:
    """Agentic mode: the model pulls its own context, and every pull is recorded."""
    p = ctx["pipeline"]
    verdict = p.triage_agentic(ctx["record"], ctx["count"])
    for call in getattr(p, "last_tool_calls", []):
        ctx["chain"].append("triage.investigate", "tool", call)
    return {"verdict": verdict, "tool_calls": len(getattr(p, "last_tool_calls", []))}


def _verdict(ctx: dict) -> dict:
    """Push mode still has to produce one; agentic mode already did."""
    v = ctx.get("verdict") or ctx["pipeline"].triage(ctx["record"], ctx["count"])
    ctx["chain"].append("triage.verdict", "verdict", {
        "fingerprint": ctx["fingerprint"], "root_cause": v.root_cause,
        "severity": v.severity, "proposed_fix": v.proposed_fix,
        "confidence": v.confidence, "needs_human_review": v.needs_human_review,
        "mode": ctx["mode"], "model": triage.MODEL,
    })
    return {"verdict": v, "confidence": v.confidence,
            "needs_human_review": v.needs_human_review}


def _route(ctx: dict) -> dict:
    v = ctx["verdict"]
    ctx["pipeline"].alert(ctx["record"], ctx["count"], v)
    return {"channel": "#oncall-escalation" if v.needs_human_review else "#auto-triage"}


def _judge(ctx: dict) -> dict:
    """Ground truth enters here and nowhere earlier.

    The grading call lives behind this node, so switching the node off actually
    saves the request rather than just omitting the record.
    """
    p = ctx["pipeline"]
    grade = getattr(p, "grade", None)
    score = grade(ctx["record"], ctx["verdict"]) if grade else None
    if score is None:
        return {"graded": False}
    ctx["chain"].append("triage.judge", "grade", {
        "fingerprint": ctx["fingerprint"],
        "cause_correct": score.cause_correct, "cause_score": score.cause_score,
        "fix_correct": score.fix_correct, "fix_score": score.fix_score,
        "reasoning": score.reasoning, "pipeline_confidence": ctx["verdict"].confidence,
    })
    return {"graded": True, "cause_score": score.cause_score, "fix_score": score.fix_score}


TRIAGE = DAG((
    Node("triage.select", fn=_select, records=("service", "error_class", "count", "model")),
    # The branch is the reason this is a graph and not a list.
    Node("triage.context", needs=("triage.select",), fn=_context,
         when=lambda c: c["mode"] == "push", records=("context_chars",)),
    Node("triage.investigate", needs=("triage.select",), fn=_investigate,
         when=lambda c: c["mode"] == "agentic", records=("tool_calls",)),
    Node("triage.verdict", needs=("triage.context", "triage.investigate"), fn=_verdict,
         records=("confidence", "needs_human_review")),
    Node("triage.route", needs=("triage.verdict",), fn=_route, records=("channel",)),
    Node("triage.judge", needs=("triage.route",), fn=_judge,
         records=("graded", "cause_score", "fix_score")),
))

ALL_NODES = tuple(n.name for n in INGEST.nodes) + tuple(n.name for n in TRIAGE.nodes)


def _self_check():
    from pathlib import Path

    # Both graphs are acyclic and ordered sensibly.
    ing = [n.name for n in INGEST.order()]
    assert ing == ["ingest.gate", "ingest.normalize", "ingest.fingerprint",
                   "ingest.count"], ing
    tri = [n.name for n in TRIAGE.order()]
    assert tri.index("triage.select") < tri.index("triage.investigate") \
        < tri.index("triage.verdict") < tri.index("triage.route") \
        < tri.index("triage.judge"), tri
    # push and agentic are siblings, not sequential steps
    assert set(TRIAGE._by_name["triage.verdict"].needs) == {"triage.context",
                                                            "triage.investigate"}

    # The map cannot silently drift from the pipeline: every node needs a home.
    tpl = Path(__file__).parent / "map_template.html"
    if tpl.exists():
        html = tpl.read_text(encoding="utf-8")
        missing = [n for n in ALL_NODES if f'"{n}"' not in html]
        assert not missing, f"nodes with no LAYOUT entry in map_template.html: {missing}"

    # The gate is what stops an INFO line costing anything.
    assert _gate({"record": {"level": "INFO"}}) is HALT
    assert _gate({"record": {"level": "ERROR"}})["passed"] is True
    assert _gate({"record": {"level": "fatal"}})["passed"] is True

    # An INFO record must not reach fingerprinting at all.
    reached = []
    probe = DAG((
        Node("ingest.gate", fn=_gate),
        Node("ingest.normalize", needs=("ingest.gate",),
             fn=lambda c: reached.append("normalize") or {}),
    ))
    probe.run({"record": {"level": "INFO", "message": "x"}})
    assert reached == [], "a dropped record walked past the gate"

    print(f"nodes self-check ok — {len(ing)} ingest + {len(tri)} triage nodes, "
          f"branch on mode, all mapped")


if __name__ == "__main__":
    from triagelab.core import store

    store.use_utf8_stdout()
    _self_check()
