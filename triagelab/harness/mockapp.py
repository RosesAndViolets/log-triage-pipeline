"""Fault simulator + graded eval harness for the triage pipeline.

Raises real exceptions, logs them through stdlib logging, and keeps the true cause
of each injected fault in a side channel the pipeline never sees. Then grades the
pipeline's verdict against that truth with an LLM judge.

Run:  python mockapp.py
"""

import itertools
import json
import logging
import sys
import urllib.request
from collections import deque
from pathlib import Path
from typing import Callable

from google.genai import errors, types
from pydantic import BaseModel, Field

from triagelab.toolserver import tools as mcp_tools  # noqa: F401  (kept so a stale import elsewhere still resolves)
from triagelab.core import nodes
from triagelab.core import store
from triagelab.core import triage

BUFFER = deque(maxlen=2000)  # every record, not just errors — INFO lines are the evidence
TRUTH: dict[str, str] = {}  # fingerprint -> what actually broke. Never shown to the pipeline.
CONTEXT_TAIL = 10  # how many recent same-service lines to include alongside the trace
_trace_seq = itertools.count(1)


def new_trace() -> str:
    return f"{next(_trace_seq):06x}"


def reset():
    """Clear the in-memory side channels between runs.

    The log store is no longer truncated here: records carry a run_id and
    get_logs filters on it, so an earlier run's lines cannot be served for this
    error. Deleting the history to avoid confusing a query was always the
    weaker fix — now the history is kept and the query is correct.
    """
    BUFFER.clear()
    TRUTH.clear()


class JudgeScore(BaseModel):
    """Two axes, because they fail independently.

    A verdict can name the defect exactly and then propose a remedy that
    accommodates it instead of removing it — observed, and invisible while only
    the cause was graded.
    """

    cause_correct: bool = Field(description="True if the diagnosis identifies the same underlying cause.")
    cause_score: float = Field(ge=0.0, le=1.0, description="How well the diagnosis matches the true cause.")
    fix_correct: bool = Field(
        description="True ONLY if the proposed fix removes the actual defect. "
        "False if it works around the defect, changes something else to accommodate it, "
        "or tells the caller to change their input."
    )
    fix_score: float = Field(ge=0.0, le=1.0, description="How well the proposed fix addresses the true defect.")
    reasoning: str = Field(description="One sentence covering both the diagnosis and the fix.")


class CapacityExceeded(Exception):
    """Raised by the worker queue when producers outpace consumers."""


# --- The faults -----------------------------------------------------------
# Each helper raises a genuine exception, so error.class, the stack and the line
# numbers are real rather than typed by hand.


def fetch_user_profile(uid: str):
    # localhost:1 is reliably closed -> real ConnectionRefusedError
    urllib.request.urlopen(f"http://127.0.0.1:1/users/{uid}", timeout=1)


def parse_api_response(body: str):
    return json.loads(body)  # truncated body -> real JSONDecodeError


def load_analytics_plugin():
    import acme_analytics_sdk  # noqa: F401  (never installed -> real ModuleNotFoundError)


def render_report(fmt: str):
    # A method removed in a library version bump -> real AttributeError
    return json.dumps({"fmt": fmt}).to_json()


def enqueue(queue: list, item: str, cap: int = 1000):
    queue.append(item)
    if len(queue) > cap:
        raise CapacityExceeded(f"queue depth {len(queue)} exceeded capacity {cap}")


class Fault:
    def __init__(self, service: str, true_cause: str, run: Callable, times: int, lead: Callable | None = None):
        self.service = service
        self.true_cause = true_cause
        self.run = run
        self.times = times
        self.lead = lead  # emits the INFO trail before the error, if any


def build_faults() -> list[Fault]:
    queue: list[str] = []

    def queue_lead(log, trace, i):
        # The buildup: only these lines reveal the queue was climbing.
        # Ramp must already be at the cap on the first pass, or only the last
        # iteration raises and the fault never reaches the frequency threshold.
        for depth in (900 + i * 100, 1000 + i * 100):
            queue.extend(["job"] * (depth - len(queue)))
            log.info("consumer lag rising, queue depth %d", len(queue), extra={"trace_id": trace})

    return [
        Fault(
            "profile-service",
            "The user-profile upstream on port 1 is not listening, so every profile lookup is refused at connect time.",
            lambda log, trace, i: fetch_user_profile(f"u{7000 + i}"),
            times=4,
            lead=lambda log, trace, i: log.info(
                "resolving profile for u%d from upstream", 7000 + i, extra={"trace_id": trace}
            ),
        ),
        Fault(
            "checkout-api",
            "The payments gateway returns a truncated JSON body, so the response parser fails mid-object.",
            lambda log, trace, i: parse_api_response('{"status":"ok","charge":{"id":' + str(i)),
            times=3,
            lead=lambda log, trace, i: log.info(
                "POST /charge accepted, awaiting gateway body", extra={"trace_id": trace}
            ),
        ),
        Fault(
            "inventory-worker",
            "Consumers are slower than producers, so the in-memory job queue grows unbounded until it passes its 1000-item cap.",
            lambda log, trace, i: enqueue(queue, f"job-{i}"),
            times=3,
            lead=queue_lead,
        ),
        Fault(
            "reporting-service",
            "The acme_analytics_sdk dependency is absent from the deployed image, so the analytics plugin cannot import.",
            lambda log, trace, i: load_analytics_plugin(),
            times=4,
            lead=lambda log, trace, i: log.info("starting nightly report build", extra={"trace_id": trace}),
        ),
        Fault(
            "reporting-service",
            "A library upgrade removed the .to_json() method, so the report renderer calls a method that no longer exists.",
            lambda log, trace, i: render_report("pdf"),
            times=2,  # stays below threshold on purpose — proves the gate holds
            lead=None,
        ),
    ]


# --- Wiring stdlib logging into the pipeline -------------------------------


class PipelineHandler(logging.Handler):
    """Maps a LogRecord to the pipeline's dict shape and feeds it in."""

    def __init__(self, pipeline: triage.TriagePipeline):
        super().__init__()
        self.pipeline = pipeline

    def emit(self, record):
        cls = record.exc_info[0].__name__ if record.exc_info else "LogRecord"
        entry = {
            "serviceName": record.name,
            "level": record.levelname,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", "-"),
            "error": {"class": cls, "stack": self.format(record) if record.exc_info else ""},
        }
        BUFFER.append(entry)
        p = self.pipeline
        if p.run_id:
            # To disk as well: the MCP server is a separate process and cannot
            # read BUFFER. Scoped by run_id, so nothing needs truncating.
            store.write_log(p.run_id, entry)
        if p.chain is None:
            p.ingest([entry])          # unbound: the old path, no record kept
            return
        nodes.INGEST.run({"record": entry, "pipeline": p, "chain": p.chain}, p.chain)


class EvalPipeline(triage.TriagePipeline):
    """Pipeline plus ground-truth grading. The pipeline half stays untouched."""

    def _fetch_context(self, log: dict) -> str:
        trace = log.get("trace_id")
        same_trace = [e for e in BUFFER if e.get("trace_id") == trace]
        tail = [e for e in BUFFER if e["serviceName"] == log["serviceName"]][-CONTEXT_TAIL:]
        seen, lines = set(), []
        for e in same_trace + tail:
            key = (e["trace_id"], e["level"], e["message"])
            if key not in seen:
                seen.add(key)
                lines.append(f"  [{e['trace_id']}] {e['level']:<5} {e['serviceName']}: {e['message']}")
        return "\n".join(lines)

    def alert(self, log: dict, count: int, v: triage.TriageVerdict):
        """Route, and show the truth for free. Grading is the judge node's job.

        Grading used to happen here, which meant switching `triage.judge` off
        removed it from the record while still spending the request. Now the
        node owns it, so disabling the node actually disables the call.
        """
        super().alert(log, count, v)
        truth = TRUTH.get(triage.fingerprint(log))
        if truth:
            print(f"      ── truth: {truth}")

    def grade(self, log: dict, v: triage.TriageVerdict) -> JudgeScore | None:
        """Called by the triage.judge node. One API call, and only if asked."""
        truth = TRUTH.get(triage.fingerprint(log))
        if not truth:
            return None
        if self.interactive:
            try:
                if input("      grade this verdict? [y/N] ").strip().lower() not in ("y", "yes"):
                    print()
                    return None
            except EOFError:  # piped run — the truth line is free, grading is not
                print()
                return None
        score = self.judge(v, truth)
        if score:
            cause = "✅ PASS" if score.cause_correct else "❌ FAIL"
            fix = "✅ PASS" if score.fix_correct else "❌ FAIL"
            print(
                f"      ── judge  cause: {cause} {score.cause_score:.2f}"
                f"   fix: {fix} {score.fix_score:.2f}"
                f"   pipeline_confidence={v.confidence:.2f}\n"
                f"                 {score.reasoning}\n"
            )
            self.scores.append((score, v.confidence))
        return score

    def judge(self, v: triage.TriageVerdict, truth: str) -> JudgeScore | None:
        if self.client is None:
            return None
        prompt = (
            "You are grading an automated log-triage system on two separate axes.\n\n"
            f"ACTUAL cause of the fault (ground truth):\n{truth}\n\n"
            f"The system's DIAGNOSIS:\n{v.root_cause}\n\n"
            f"The system's PROPOSED FIX:\n{v.proposed_fix}\n\n"
            "1. Diagnosis: did it identify the same underlying cause? Judge the substance, "
            "not the wording. Partial credit for a cause that is directionally right but "
            "misses the mechanism.\n\n"
            "2. Fix: would it remove the defect described in the ground truth? The correct "
            "remedy undoes what actually broke. A fix is WRONG — however plausible it "
            "sounds — if it works around the defect, adapts other code or data to tolerate "
            "it, or asks the caller to change their input. Treating the broken behaviour as "
            "intended and patching elsewhere is the specific failure to catch here.\n\n"
            "Grade the two independently: a correct diagnosis with a wrong fix is a real "
            "and common outcome, and must score high on one and low on the other."
        )
        try:
            resp = self.client.models.generate_content(
                model=triage.MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json", response_schema=JudgeScore
                ),
            )
            return resp.parsed
        except errors.APIError as e:
            print(f"      ── judge unavailable: API {e.code}")
            return None
        finally:
            triage.time.sleep(triage.RPM_SLEEP)  # second call per signature — stay under 15 RPM

    scores: list = []


# --- Emission --------------------------------------------------------------


def simulate(pipeline: triage.TriagePipeline):
    """Fire every fault, interleaved, so the buffer looks like a real multi-service stream."""
    handler = PipelineHandler(pipeline)
    faults = build_faults()
    loggers = {}
    for f in faults:
        log = loggers.setdefault(f.service, logging.getLogger(f.service))
        log.setLevel(logging.INFO)
        log.propagate = False
        # Rebind every call: logging.getLogger is global and outlives the pipeline,
        # so a stale handler would keep feeding a previous run's instance.
        log.handlers = [handler]

    # Round-robin the faults so traces from different services interleave.
    for i in range(max(f.times for f in faults)):
        for f in faults:
            if i >= f.times:
                continue
            log, trace = loggers[f.service], new_trace()
            if f.lead:
                f.lead(log, trace, i)
            try:
                f.run(log, trace, i)
            except Exception as e:
                log.error(str(e), exc_info=True, extra={"trace_id": trace})
                TRUTH[triage.fingerprint(BUFFER[-1])] = f.true_cause


def _self_check():
    reset()
    p = EvalPipeline(threshold=3)
    simulate(p)

    # Real exceptions produced real class names.
    classes = {e["error"]["class"] for e in BUFFER if e["level"] == "ERROR"}
    # URLError, not ConnectionRefusedError: urllib wraps the refusal, as it does in production.
    assert {"URLError", "JSONDecodeError", "CapacityExceeded",
            "ModuleNotFoundError", "AttributeError"} <= classes, classes

    # The gate: 4 signatures over threshold, the 2x AttributeError below it.
    hot = {fp for fp, n in p.counts.items() if n >= p.threshold}
    assert len(hot) == 4, p.counts
    below = [fp for fp, n in p.counts.items() if n < p.threshold]
    assert any(p.samples[fp]["error"]["class"] == "AttributeError" for fp in below), below

    # Every hot signature has ground truth to grade against.
    assert all(fp in TRUTH for fp in hot), hot - set(TRUTH)

    # Context actually carries the queue-depth trail, which appears in no error message.
    acc = next(fp for fp in hot if p.samples[fp]["error"]["class"] == "CapacityExceeded")
    ctx = p._fetch_context(p.samples[acc])
    assert "consumer lag rising" in ctx, ctx
    assert "consumer lag rising" not in p.samples[acc]["message"]

    # A bound run must leave a chain that reads as the ingestion actually went:
    # gate/normalize/fingerprint/count for real errors, and the INFO lines
    # halted at the gate rather than walking the whole graph.
    import shutil
    import tempfile

    real = (store.LOGS_DB, store.RUNS_DB)
    tmp = Path(tempfile.mkdtemp())
    store.LOGS_DB, store.RUNS_DB = tmp / "l.db", tmp / "r.db"
    try:
        reset()
        with store.open_run(model="test", mode="stub", run_id="sc1") as (rid, ch):
            bound = EvalPipeline(threshold=3).bind(rid, ch)
            bound.client = None          # no API key needed to exercise the graph
            simulate(bound)

        d = store.load_run("sc1")
        assert d["intact"], "the chain a clean run wrote does not verify"
        kinds = [(e["node"], e["kind"]) for e in d["events"]]
        assert ("ingest.fingerprint", "exit") in kinds, kinds[:8]

        halted = [e for e in d["events"]
                  if e["node"] == "ingest.gate" and e["payload"].get("halted")]
        errors = [e for e in BUFFER if e["level"] == "ERROR"]
        assert len(halted) == len(BUFFER) - len(errors), \
            f"{len(halted)} halted vs {len(BUFFER) - len(errors)} non-error records"
        # nothing below ERROR reached fingerprinting
        assert sum(1 for n, k in kinds if n == "ingest.fingerprint" and k == "exit") \
            == len(errors)
        # and the logs went to the store, scoped to this run
        assert len(store.query_logs(run_id="sc1", limit=10000)) == len(BUFFER)
    finally:
        store.LOGS_DB, store.RUNS_DB = real
        shutil.rmtree(tmp, ignore_errors=True)

    print("self-check ok — 5 real exception types, 4 signatures over threshold, "
          "context wired, chain records ingestion")


def main(argv: list[str]):
    """Entry point for `run.py mock` and for the server's subprocess launch."""
    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    agentic = "--agentic" in argv
    pick = int(opt("--pick", 0)) or None
    disabled = frozenset(a for a in opt("--disable", "").split(",") if a)
    # The server picks the id up front so it can poll before we open the run.
    run_id_in = opt("--run-id", "")

    reset()
    EvalPipeline.scores = []
    with store.open_run(model=triage.MODEL, mode="agentic" if agentic else "push", run_id=run_id_in,
                        condition={"harness": "mock", "pick": pick,
                                   "disabled": sorted(disabled)}) as (run_id, chain):
        pipeline = EvalPipeline(threshold=3).bind(run_id, chain)
        simulate(pipeline)
        print(f"\nsimulated {len(BUFFER)} log records across "
              f"{len({e['serviceName'] for e in BUFFER})} services")
        pipeline.run(interactive=pick is None, agentic=agentic,
                     pick=pick, disabled=disabled)
        _report(pipeline)
        print(f"\nrun {run_id} recorded — replay with:"
              f"\n  python run.py export --run {run_id}")
    return run_id


def _report(pipeline):
    """The graded summary. Only meaningful when something was actually judged."""
    if not pipeline.scores:
        return
    n = len(pipeline.scores)
    s = [x for x, _ in pipeline.scores]
    print(f"\n{'='*66}\n"
          f"GRADED {n}   "
          f"cause {sum(x.cause_correct for x in s)}/{n} correct, "
          f"mean {sum(x.cause_score for x in s)/n:.2f}   "
          f"fix {sum(x.fix_correct for x in s)}/{n} correct, "
          f"mean {sum(x.fix_score for x in s)/n:.2f}\n"
          f"       mean pipeline confidence "
          f"{sum(c for _, c in pipeline.scores)/n:.2f}"
          # The gap between these is the number worth watching: a pipeline that
          # diagnoses well and prescribes badly looks perfect on cause alone.
          f"   (cause-minus-fix gap "
          f"{(sum(x.cause_score for x in s) - sum(x.fix_score for x in s))/n:+.2f})"
          f"\n{'='*66}")


if __name__ == "__main__":
    store.use_utf8_stdout()
    if "--no-self-check" not in sys.argv:
        _self_check()
    main(sys.argv[1:])
