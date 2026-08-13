"""Automated log triage pipeline: dedup -> normalize -> Gemini triage -> mock Slack."""

import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field, model_validator

# Free tier is 20 requests/day PER MODEL, so the model you pick is a budget choice.
# Override for a stronger verdict: TRIAGE_MODEL=gemini-3.6-flash python mockapp.py
MODEL = os.getenv("TRIAGE_MODEL", "gemini-3.1-flash-lite")
# Base pacing for the free tier (15 req/min); the client's retry_options absorb
# the rest. On a paid key: TRIAGE_RPM_SLEEP=0. Quota limits live in env vars,
# never in code, so the work machine lifts them without an edit.
RPM_SLEEP = float(os.getenv("TRIAGE_RPM_SLEEP", "4"))
# Each tool round-trip is its own API request, so this is a budget, not a timeout.
AGENTIC_TOOL_BUDGET = int(os.getenv("TRIAGE_TOOL_BUDGET", "4"))


class TriageVerdict(BaseModel):
    root_cause: str = Field(description="The underlying cause of the error.")
    severity: str = Field(description="Severity level: CRITICAL, HIGH, MEDIUM, LOW.")
    proposed_fix: str = Field(
        description="Actionable steps or code changes to fix the issue."
    )
    confidence: float = Field(
        ge=0.0, le=1.0, description="Confidence score from 0.0 to 1.0."
    )
    needs_human_review: bool = Field(description="True if confidence is below 0.8.")

    @model_validator(mode="after")
    def _enforce_review_flag(self):
        # Never trust the model to compute this: it is a routing decision.
        self.needs_human_review = self.confidence < 0.8
        return self


def normalize(text: str) -> str:
    """Scrub volatile data so the same error hashes identically every time."""
    text = re.sub(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", "<TS>", text)
    text = re.sub(r"\b[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\b", "<UUID>", text)
    text = re.sub(r"\d+", "<N>", text)  # no \b: must also catch "5031ms", "line42"
    return text.strip()


def fingerprint(log: dict) -> str:
    """Stable MD5 over serviceName + error.class + message."""
    key = f"{log['serviceName']}|{log['error']['class']}|{normalize(log['message'])}"
    return hashlib.md5(key.encode()).hexdigest()


class TriagePipeline:
    # Where the agentic code tools may look. "" = the whole repo root; the real
    # harness narrows this to "targets" so the agent can never search the
    # harness's own FAULTS table — the answer sheet. Class attribute so a
    # subclass can override it without fighting __init__.
    tool_jail = ""

    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self.agentic = False
        # Set by bind() when a run is opened. Without them the pipeline still
        # works — it just leaves no record, which is the old behaviour.
        self.run_id = ""
        self.chain = None
        self.disabled: frozenset = frozenset()
        self.interactive = True   # the grade prompt only makes sense with a tty
        self.last_tool_calls: list[dict] = []
        # Identity of what the last _fetch_context call retrieved (files, log
        # lines) — same pattern as last_tool_calls, recorded by triage.context.
        self.last_context_meta: dict = {}
        self.last_score = None
        self.counts: Counter[str] = Counter()
        self.samples: dict[str, dict] = {}
        # The SDK retries 429/503 with exponential backoff itself — no hand-rolled retry loop.
        self.client = (
            genai.Client(
                http_options=types.HttpOptions(
                    retry_options=types.HttpRetryOptions(
                        attempts=5, initial_delay=10, max_delay=90, http_status_codes=[429, 503]
                    )
                )
            )
            if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
            else None
        )

    def bind(self, run_id: str, chain, disabled=frozenset()):
        """Attach this pipeline to an open run, so its steps land on the chain.

        `disabled` is set here and nowhere else. It used to be set in run(),
        which executes after every record has already been ingested — so ingest
        nodes could never be switched off, however clearly the UI said they were.
        Binding is what attaches the chain, so nothing can be ingested before
        this point, which makes that ordering bug structurally impossible.
        """
        self.run_id, self.chain = run_id, chain
        self.disabled = frozenset(disabled)
        return self

    def ingest(self, logs: list[dict]):
        """Stage 1 & 2: dedup gate + normalization."""
        for log in logs:
            if log.get("level") not in ("ERROR", "FATAL"):
                continue
            fp = fingerprint(log)
            self.counts[fp] += 1
            self.samples.setdefault(fp, log)

    def run(self, interactive: bool = False, agentic: bool = False,
            pick: int | str | None = None):
        """Stage 3 & 4: LLM triage loop, then routing.

        `pick` triages the Nth hot signature (1-indexed) with no prompt. A run
        started by the server has no tty, so without this it would ingest and
        then decline to triage anything at all. A service name works too, and
        is resolved here — the only place the hot list exists — so a UI can say
        "the checkout-api error" without guessing at menu order.

        Note there is no `disabled` argument: it belongs to bind(), because by
        the time this is called the ingest graph has already run.
        """
        self.agentic = agentic
        self.interactive = interactive and pick is None
        hot = [fp for fp, n in self.counts.items() if n >= self.threshold]
        print(
            f"\n{len(self.counts)} unique signatures, {len(hot)} over threshold ({self.threshold}).\n"
        )
        if isinstance(pick, str):
            byname = [i for i, fp in enumerate(hot, 1)
                      if self.samples[fp]["serviceName"] == pick]
            if not byname:
                print(f"   ⚠️  --pick {pick!r}: no hot signature from that service "
                      f"({', '.join(self.samples[fp]['serviceName'] for fp in hot) or 'none'})")
                return
            pick = byname[0]
        if pick is not None:
            if not 1 <= pick <= len(hot):
                print(f"   ⚠️  --pick {pick} is out of range: {len(hot)} over threshold")
                return
            print(f"picking {pick} of {len(hot)} — model: {MODEL}")
            return self._triage_one(hot[pick - 1])
        if interactive:
            return self._run_interactive(hot)
        for fp in hot:
            self._triage_one(fp)
            time.sleep(RPM_SLEEP)

    def _triage_one(self, fp: str):
        log, n = self.samples[fp], self.counts[fp]
        print(f"🔎 Triaging {fp[:12]} — {log['serviceName']} / {log['error']['class']} ({n}x)")
        if self.chain is None:          # unbound: no run open, so nothing to record
            verdict = self.triage_agentic(log, n) if self.agentic else self.triage(log, n)
            self.alert(log, n, verdict)
            return
        from triagelab.core import nodes   # local: only the DAG path needs it

        nodes.TRIAGE.run({
            "pipeline": self, "chain": self.chain, "fingerprint": fp,
            "mode": "agentic" if self.agentic else "push",
        }, self.chain, disabled=self.disabled)

    def _run_interactive(self, hot: list[str]):
        """One API call per pick. The free tier is 20/day — nothing is spent unasked."""
        done: set[str] = set()
        while True:
            print(f"signatures over threshold ({self.threshold}) — model: {MODEL}")
            for i, fp in enumerate(hot, 1):
                log = self.samples[fp]
                mark = "  [done]" if fp in done else ""
                print(
                    f"  {i}  {log['serviceName']:<20} {log['error']['class']:<22}"
                    f" {self.counts[fp]}x{mark}"
                )
            try:
                choice = input("pick a number to triage, or q to quit: ").strip()
            except EOFError:  # piped/redirected run — spend nothing
                print("(no tty — nothing triaged)")
                return
            if choice.lower() in ("q", "quit", ""):
                return
            if not choice.isdigit() or not 1 <= int(choice) <= len(hot):
                print(f"  ? pick 1-{len(hot)} or q\n")
                continue
            fp = hot[int(choice) - 1]
            print()
            self._triage_one(fp)
            done.add(fp)
            time.sleep(RPM_SLEEP)

    def _fetch_context(self, log: dict) -> str:
        """Related log lines. Base pipeline has no log store — subclass to supply one.

        Implementations must also set self.last_context_meta to say *what* was
        retrieved, not just return it — a wrong verdict is only diagnosable if
        the chain can show whether the right context was ever in the packet.
        """
        self.last_context_meta = {}
        return ""

    def triage(self, log: dict, count: int) -> TriageVerdict:
        if self.client is None:
            print("   ⚠️  No GEMINI_API_KEY — using stub verdict.")
            return TriageVerdict(
                root_cause=f"[stub] {log['error']['class']} in {log['serviceName']}",
                severity="MEDIUM",
                proposed_fix="Set GEMINI_API_KEY for a real verdict.",
                confidence=0.5,
                needs_human_review=True,
            )
        prompt = (
            f"Triage this production error. It fired {count} times.\n\n"
            f"service: {log['serviceName']}\n"
            f"class: {log['error']['class']}\n"
            f"message: {log['message']}\n"
            f"stack: {log['error'].get('stack', 'n/a')}\n"
        )
        context = self._fetch_context(log)
        if context:
            prompt += f"\nrelated log lines leading up to the error:\n{context}\n"
        try:
            resp = self.client.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=TriageVerdict,
                ),
            )
            return resp.parsed
        except errors.APIError as e:  # ClientError (4xx) + ServerError (5xx)
            # ponytail: no retry — a failed triage escalates instead of vanishing.
            # Add tenacity-style backoff if 429s become routine.
            print(f"   ⚠️  API {e.code}: {e.message}")
            return TriageVerdict(
                root_cause=f"Triage failed: API {e.code}. Error unanalyzed.",
                severity="UNKNOWN",
                proposed_fix="Re-run triage for this signature.",
                confidence=0.0,
                needs_human_review=True,
            )

    # --- Agentic path: the model pulls its own context through MCP -----------

    def _agentic_prompt(self, log: dict, count: int) -> str:
        return (
            f"You are triaging a production error that fired {count} times.\n\n"
            f"service: {log['serviceName']}\n"
            f"class: {log['error']['class']}\n"
            f"message: {log['message']}\n"
            f"trace_id: {log.get('trace_id', 'unknown')}\n"
            f"stack:\n{log['error'].get('stack', 'n/a')}\n\n"
            "This traceback is your starting point, not your evidence. You have tools:\n"
            "  read_source   — read the actual code behind any frame above\n"
            "  search_code   — find callers, definitions, similar patterns\n"
            "  get_logs      — pull the log lines around this error by trace_id or service\n"
            "  line_history  — find when a specific line last changed, and in which commit\n\n"
            "Investigate before concluding. Read the code at the deepest frame that is not "
            "standard library. When you know what is wrong, call submit_verdict exactly once. "
            "Do not answer in prose — the verdict only counts if it arrives through the tool."
        )

    def triage_agentic(self, log: dict, count: int) -> TriageVerdict:
        """Let the model fetch its own context, then submit a verdict as a tool call.

        Tools and response_schema do not compose — with both set the API returns a
        dangling function_call and `parsed` is None. So the schema is enforced on our
        side: submit_verdict's arguments are fed straight into the Pydantic model.
        """
        if self.client is None:
            return self.triage(log, count)  # no key: fall back to the stub
        return asyncio.run(self._agentic_call(log, count))

    async def _agentic_call(self, log: dict, count: int) -> TriageVerdict:
        # Imported here so the non-agentic pipeline never requires the mcp package.
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        captured: dict[str, TriageVerdict] = {}

        def submit_verdict(root_cause: str, severity: str, proposed_fix: str,
                           confidence: float) -> str:
            """Submit the final triage verdict. Call this exactly once, when you are done.

            Args:
                root_cause: The underlying cause, naming the specific code if you found it.
                severity: CRITICAL, HIGH, MEDIUM or LOW.
                proposed_fix: Concrete actionable steps.
                confidence: 0.0 to 1.0.
            """
            captured["v"] = TriageVerdict(
                root_cause=root_cause, severity=severity, proposed_fix=proposed_fix,
                confidence=max(0.0, min(1.0, confidence)),
                needs_human_review=False,  # the validator recomputes this
            )
            return "verdict recorded"

        # Launched as a module, not a file path: the server imports triagelab, so it
        # needs the repo root importable. cwd anchors that on Windows too, where a
        # bare script path would not resolve the package at all.
        repo_root = Path(__file__).resolve().parents[2]
        # env must be passed explicitly: MCP's default environment strips
        # custom variables, and the jail has to survive into the subprocess.
        from mcp.client.stdio import get_default_environment
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "triagelab.toolserver.server"],
            cwd=str(repo_root),
            env={**get_default_environment(), "TRIAGE_TOOL_JAIL": self.tool_jail}
                if self.tool_jail else None,
        )
        # errlog to devnull: the server logs every request to stderr, which is noise
        # in front of a verdict. The tool calls are printed below instead.
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            async with stdio_client(params, errlog=quiet) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    try:
                        resp = await self.client.aio.models.generate_content(
                            model=MODEL,
                            contents=self._agentic_prompt(log, count),
                            # A dict, not GenerateContentConfig: the object branch runs
                            # config.model_copy(deep=True), and a live ClientSession
                            # holds an _asyncio.Task, which cannot be pickled. The dict
                            # branch constructs instead of copying.
                            config={
                                "tools": [session, submit_verdict],
                                "automatic_function_calling":
                                    types.AutomaticFunctionCallingConfig(
                                        # +1: submit_verdict is itself a function call
                                        # and counts against this. Without the slot the
                                        # model spends the whole budget investigating
                                        # and is cut off before it can answer.
                                        maximum_remote_calls=AGENTIC_TOOL_BUDGET + 1
                                    ),
                            },
                        )
                    except errors.APIError as e:
                        print(f"   ⚠️  API {e.code}: {e.message}")
                        return self._failed_verdict(f"API {e.code}")

        self._print_tool_calls(resp)
        return captured.get("v") or self._fallback(resp)

    def _print_tool_calls(self, resp):
        """Show what the model pulled, and keep it — this is the agent's evidence.

        Previously this only printed. The calls and what came back are the most
        interesting thing the agentic path produces, and they were being thrown
        away the moment the line scrolled past.
        """
        self.last_tool_calls = []
        pending: dict[str, dict] = {}
        for turn in resp.automatic_function_calling_history or []:
            for part in turn.parts or []:
                if fc := getattr(part, "function_call", None):
                    args = dict(fc.args or {})
                    rec = {"name": fc.name, "args": args, "result": ""}
                    pending[fc.name] = rec
                    self.last_tool_calls.append(rec)
                    pretty = ", ".join(f"{k}={v!r}" for k, v in args.items())
                    print(f"      ↳ {fc.name}({pretty[:120]})")
                if fr := getattr(part, "function_response", None):
                    # Pair the answer back onto its call so the chain records both.
                    rec = pending.get(fr.name)
                    if rec is not None:
                        out = (fr.response or {}).get("result", fr.response)
                        # An MCP tool answers with a CallToolResult, whose str()
                        # is an object repr. The text inside is the evidence —
                        # the thing the replay needs to show — so unwrap it.
                        content = getattr(out, "content", None)
                        if isinstance(content, list):
                            out = "\n".join(getattr(c, "text", str(c)) for c in content)
                        rec["result"] = str(out)[:1500]

    def _fallback(self, resp) -> TriageVerdict:
        """The model spent its tool budget without submitting. Escalate, never guess."""
        text = (getattr(resp, "text", "") or "").strip()
        print("   ⚠️  no submit_verdict call — escalating")
        return self._failed_verdict("no verdict submitted", detail=text[:400])

    @staticmethod
    def _failed_verdict(why: str, detail: str = "") -> TriageVerdict:
        return TriageVerdict(
            root_cause=f"Triage incomplete ({why}). {detail}".strip(),
            severity="UNKNOWN",
            proposed_fix="Re-run triage for this signature.",
            confidence=0.0,  # validator flips needs_human_review to True
            needs_human_review=True,
        )

    def alert(self, log: dict, count: int, v: TriageVerdict):
        """Stage 5: mock Slack routing."""
        head = f"{log['serviceName']} · {log['error']['class']} · {count}x"
        if v.needs_human_review:
            print(
                f"   🚨 #oncall-escalation | [HUMAN REVIEW] {v.severity} — {head}\n"
                f"      cause: {v.root_cause}\n"
                f"      confidence: {v.confidence:.2f} (below 0.8 — do not auto-apply)\n"
                f"      suggested: {v.proposed_fix}\n"
                f"      → assign an engineer\n"
            )
        else:
            print(
                f"   ✅ #auto-triage | {v.severity} — {head}\n"
                f"      cause: {v.root_cause}\n"
                f"      fix: {v.proposed_fix}  (confidence {v.confidence:.2f})\n"
            )


def generate_mock_logs() -> list[dict]:
    """Mock stream. Two signatures cross a threshold of 3; the rest do not."""

    def err(svc, cls, msg, stack, level="ERROR"):
        return {
            "serviceName": svc,
            "level": level,
            "message": msg,
            "error": {"class": cls, "stack": stack},
        }

    logs = []
    # 4x — same signature, volatile ids differ (normalization must collapse these)
    for uid in (
        "123a4567-e89b-12d3-a456-426614174000",
        "987b6543-e21b-34d3-b890-426614174111",
        "555c4444-e11b-22d2-c777-426614174222",
        "77df1290-aa1b-49f1-9d0c-426614174333",
    ):
        logs.append(
            err(
                "auth-service",
                "NullPointerException",
                f"token was None for user_id={uid} at 2026-08-10T10:00:00Z",
                "at auth.middleware.verify(middleware.py:42)",
            )
        )
    # 3x — hits threshold exactly
    for ms in (5031, 5044, 5102):
        logs.append(
            err(
                "checkout-api",
                "TimeoutError",
                f"upstream payments-gateway did not respond within {ms}ms",
                "at checkout.charge(charge.py:118)",
            )
        )
    # 2x — below threshold, must be skipped
    for i in (7, 9):
        logs.append(
            err(
                "inventory-worker",
                "IntegrityError",
                f"duplicate key value violates unique constraint sku_idx (attempt {i})",
                "at inventory.sync(sync.py:64)",
            )
        )
    # noise that must never reach the LLM
    logs.append(
        {
            "serviceName": "auth-service",
            "level": "INFO",
            "message": "user logged in successfully",
            "error": {"class": "", "stack": ""},
        }
    )
    logs.append(
        err(
            "cdn-edge",
            "ConnectionReset",
            "peer closed connection mid-stream",
            "n/a",
            level="WARN",
        )
    )
    return logs


def _self_check():
    logs = generate_mock_logs()
    p = TriagePipeline(threshold=3)
    p.ingest(logs)
    assert sum(p.counts.values()) == 9, p.counts  # 4 + 3 + 2 errors, noise dropped
    assert len(p.counts) == 3, p.counts  # collapsed into 3 signatures
    hot = sorted(n for n in p.counts.values() if n >= 3)
    assert hot == [3, 4], hot  # inventory (2x) stays below
    assert TriageVerdict(
        root_cause="a",
        severity="LOW",
        proposed_fix="b",
        confidence=0.79,
        needs_human_review=False,
    ).needs_human_review
    assert not TriageVerdict(
        root_cause="a",
        severity="LOW",
        proposed_fix="b",
        confidence=0.81,
        needs_human_review=True,
    ).needs_human_review

    # An unbound pipeline must still work — binding a run is what adds the
    # record, not what makes triage function.
    assert p.chain is None and p.run_id == ""
    p.bind("r1", object())
    assert p.run_id == "r1" and p.chain is not None

    # Tool results must be recorded as their text, not as an object repr —
    # the replay's "what did read_source actually read" panel depends on it.
    from types import SimpleNamespace as NS
    call = NS(name="read_source", args={"path": "x.py"})
    mcpish = NS(content=[NS(text="line one"), NS(text="line two")])
    resp = NS(automatic_function_calling_history=[
        NS(parts=[NS(function_call=call, function_response=None)]),
        NS(parts=[NS(function_call=None,
                     function_response=NS(name="read_source",
                                          response={"result": mcpish}))]),
    ])
    p._print_tool_calls(resp)
    assert p.last_tool_calls[0]["result"] == "line one\nline two", p.last_tool_calls
    print("self-check ok")


if __name__ == "__main__":
    from triagelab.core import store

    store.use_utf8_stdout()
    _self_check()
    p = TriagePipeline(threshold=3)
    p.ingest(generate_mock_logs())
    p.run()
