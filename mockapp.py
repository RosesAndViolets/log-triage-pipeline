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
from typing import Callable

from google.genai import errors, types
from pydantic import BaseModel, Field

import mcp_tools
import triage

BUFFER = deque(maxlen=2000)  # every record, not just errors — INFO lines are the evidence
TRUTH: dict[str, str] = {}  # fingerprint -> what actually broke. Never shown to the pipeline.
CONTEXT_TAIL = 10  # how many recent same-service lines to include alongside the trace
_trace_seq = itertools.count(1)


def new_trace() -> str:
    return f"{next(_trace_seq):06x}"


def reset():
    """Start a run clean — in-memory buffers and the on-disk log store both.

    Without truncating the store, get_logs would serve the previous run's lines
    and the model would reason about an error that is no longer there.
    """
    BUFFER.clear()
    TRUTH.clear()
    mcp_tools.LOG_STORE.write_text("")


class JudgeScore(BaseModel):
    correct: bool = Field(description="True if the verdict identifies the same underlying cause.")
    score: float = Field(ge=0.0, le=1.0, description="How well the verdict matches the true cause.")
    reasoning: str = Field(description="One sentence explaining the score.")


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
        # Also to disk: the MCP server is a separate process and cannot read BUFFER.
        with open(mcp_tools.LOG_STORE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        self.pipeline.ingest([entry])  # ingest() already drops non-ERROR levels


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
        super().alert(log, count, v)
        truth = TRUTH.get(triage.fingerprint(log))
        if not truth:
            return
        print(f"      ── truth: {truth}")
        try:
            if input("      grade this verdict? [y/N] ").strip().lower() not in ("y", "yes"):
                print()
                return
        except EOFError:  # piped run — the truth line above is free, grading is not
            print()
            return
        score = self.judge(v, truth)
        if score:
            mark = "✅ PASS" if score.correct else "❌ FAIL"
            print(
                f"      ── judge: {mark}  match={score.score:.2f}  "
                f"pipeline_confidence={v.confidence:.2f}\n"
                f"                 {score.reasoning}\n"
            )
            self.scores.append((score, v.confidence))

    def judge(self, v: triage.TriageVerdict, truth: str) -> JudgeScore | None:
        if self.client is None:
            return None
        prompt = (
            "You are grading an automated log-triage system.\n\n"
            f"ACTUAL cause of the fault (ground truth):\n{truth}\n\n"
            f"The system's diagnosis:\n{v.root_cause}\n\n"
            "Did the system identify the same underlying cause? Judge the substance, "
            "not the wording. Partial credit for a cause that is directionally right "
            "but misses the mechanism."
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

    print("self-check ok — 5 real exception types, 4 signatures over threshold, context wired")


if __name__ == "__main__":
    _self_check()

    reset()
    EvalPipeline.scores = []
    pipeline = EvalPipeline(threshold=3)
    simulate(pipeline)
    print(f"\nsimulated {len(BUFFER)} log records across "
          f"{len({e['serviceName'] for e in BUFFER})} services")
    pipeline.run(interactive=True, agentic="--agentic" in sys.argv)

    if pipeline.scores:
        n = len(pipeline.scores)
        correct = sum(s.correct for s, _ in pipeline.scores)
        print(f"\n{'='*60}\nGRADED: {correct}/{n} correct   "
              f"mean judge score {sum(s.score for s, _ in pipeline.scores)/n:.2f}   "
              f"mean pipeline confidence {sum(c for _, c in pipeline.scores)/n:.2f}\n{'='*60}")
