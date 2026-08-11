# Stateless MCP toolserver for the triage pipeline

> **Status: planned, not implemented.** Written 2026-08-11. No code from this document
> exists yet — `mcp_tools.py` and `mcp_server.py` have not been created, and `triage.py`
> has no agentic path. The probe findings below are verified against the live API; the
> code blocks are design sketches, not copies of working files.
>
> Design decisions already settled: verdict returns via a `submit_verdict` tool (not
> `response_schema`), tool loop capped at `maximum_remote_calls=4`.
>
> Start at **Files** for the work breakdown, **Verification** for how to prove it works.

## Context

**Correcting the premise first, because it changes the design.** The triage LLM *does*
already see source code — but only in `realapp.py`, and only what we chose for it:
`render_source()` grabs the deepest 3 traceback frames, ±4 lines each, at log time.
`mockapp.py` sends log lines only. The base `triage.py` sends nothing.

So the gap isn't "can't read source". It's that **context is pushed, never pulled.** The
model gets a fixed packet decided before the call and cannot ask for anything else — it
can't open the file the failing function calls, widen the window, grep for other callers,
or check when a line last changed. If the answer lies one frame outside our window, it
guesses.

MCP turns push into pull. That is the actual change.

### One honest caveat, then I build what you asked

For a single-process prototype, `config.tools=[my_function]` gives the same pull behaviour
with **zero new dependencies and no server** — the SDK does automatic function calling over
plain Python callables. MCP earns its keep when the tools must be shared across hosts or
agents, survive independently of this process, or be reused by other clients (Claude Code
can consume the same server).

Since you want MCP: the tools get written as **plain functions** and MCP becomes a thin
wrapper over them. One implementation, two surfaces, and the offline tests call the
functions directly with no server or API involved.

### Probe findings (verified, not assumed)

- `mcp` is **not installed** — a genuine new dependency.
- The SDK accepts a local `mcp.ClientSession` in `config.tools` (`_extra_utils.py:577`) and
  runs the tool loop client-side. This is the route that works for a local stdio server.
- `types.McpServer` exists but only carries `streamable_http_transport` — that's the API
  connecting *outbound* to a public URL. Useless for localhost. **Not the route to take.**
- **Tools and `response_schema` do not compose.** With both set, the response came back as a
  dangling `function_call` part and `parsed` was `None`. This is why the verdict arrives as
  a tool call instead.
- Free tier has *two* limits: 20/day **and** 15/minute, both per model.

## Design

**Stateless** means the server holds nothing between calls: every tool takes everything it
needs as arguments (path, line, trace_id) and reads from disk. Restarting it loses nothing.

That forces one real change: **logs must be persisted.** A separate process cannot read the
in-memory `BUFFER` deque. `PipelineHandler.emit()` will append each entry to `run.jsonl`,
which the `get_logs` tool queries. This also closes known-limitation #3 from the README.

### `mcp_tools.py` — the actual capability, no MCP involved

```python
ROOT = Path(__file__).parent.resolve()

def _safe(path: str) -> Path:
    """Trust boundary: the model picks these paths. Never leave the repo."""
    p = (ROOT / path).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError(f"path escapes repo root: {path}")
    return p

def read_source(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read numbered source lines from a file in the repo."""

def search_code(pattern: str, glob: str = "**/*.py", max_hits: int = 40) -> str:
    """Regex search across the repo. Returns file:line: text hits."""

def get_logs(trace_id: str = "", service: str = "", level: str = "",
             limit: int = 40) -> str:
    """Query the persisted log store, filtered by trace/service/level."""

def line_history(path: str, line: int) -> str:
    """git log -L for one line: what changed it, when, and in which commit."""
```

Path jailing is not optional — the model chooses these arguments, so `_safe()` is a trust
boundary and gets its own test.

`line_history` is the one that doesn't exist in any form today, and it's the highest-value
tool for real triage: "this line last changed 2 hours before the first occurrence" is
usually the whole answer.

### `mcp_server.py` — thin stdio wrapper

```python
from mcp.server.fastmcp import FastMCP
import mcp_tools

mcp = FastMCP("triage-tools")
for fn in (mcp_tools.read_source, mcp_tools.search_code,
           mcp_tools.get_logs, mcp_tools.line_history):
    mcp.tool()(fn)

if __name__ == "__main__":
    mcp.run()          # stdio; no port, no session state
```

### `triage.py` — the agentic path

`submit_verdict` is registered as a tool; the model calls it last and Pydantic validates
the arguments. Capped at 4 tool round-trips.

```python
def triage_agentic(self, log, count, session):
    captured = {}
    def submit_verdict(root_cause: str, severity: str, proposed_fix: str,
                       confidence: float) -> str:
        """Submit the final triage verdict. Call this once, last."""
        captured["v"] = TriageVerdict(
            root_cause=root_cause, severity=severity, proposed_fix=proposed_fix,
            confidence=confidence, needs_human_review=False,  # validator recomputes
        )
        return "verdict recorded"

    resp = self.client.models.generate_content(
        model=MODEL, contents=self._agentic_prompt(log, count),
        config=types.GenerateContentConfig(
            tools=[session, submit_verdict],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=4),
        ),
    )
    return captured.get("v") or self._fallback(resp)   # model may never submit
```

The `_fallback` matters: if the model burns its 4 calls without submitting, we must still
route something to a human rather than crash. It returns a `confidence=0.0` verdict, which
the existing validator flips to `needs_human_review=True` automatically.

The existing single-shot `triage()` stays as the default. Agentic mode is opt-in per run
(`--agentic`), because it costs up to 5× the requests.

### Prompt change

The current prompt hands over a context blob. The agentic prompt instead says what's
*available*: "You have tools to read source, search code, query logs, and get line history.
Investigate, then call submit_verdict." The traceback still goes in the prompt — that's the
starting point, not the whole evidence.

## What else this MCP should do for a large-scale app

Implementing 4. These are the roles worth adding as the app grows, ranked by how often they
turn out to be the answer:

1. **Deploy correlation** — `deploys_before(timestamp, service)`. The single highest-signal
   tool in real incident response: most novel errors start at a release boundary. Pairs with
   `line_history` to answer "did the change that touched this line ship just before it broke?"
2. **Ownership lookup** — `owners(path)` from CODEOWNERS. Turns `needs_human_review` from a
   flag into an actual routed page.
3. **Incident memory** — `similar_past_verdicts(fingerprint)`. At scale the same signature
   recurs for months; retrieving what fixed it last time beats re-diagnosing. This is where
   persisting verdicts pays off.
4. **Cross-service correlation** — `related_errors(window, exclude_service)`. Distinguishes
   "this service is broken" from "its upstream is broken", which changes the severity and
   the page target.
5. **Config and flag state** — `flag_state(name, timestamp)`. A large fraction of production
   incidents are configuration, not code, and are invisible in a traceback.
6. **Metrics** — `error_rate(service, window)`, latency percentiles. Turns a frequency count
   into a blast-radius estimate, which is what `severity` should actually be derived from.
7. **Runbooks** — `runbook(error_class)`. Cheap retrieval, makes `proposed_fix` match house
   convention instead of generic advice.

The pattern behind the list: the pipeline currently reasons only about *the error*. Every
tool above adds an axis it can't see today — time (deploys, history), people (ownership),
precedent (past incidents), breadth (correlation, metrics), and configuration.

## Files

| File | Change |
|---|---|
| `mcp_tools.py` | new — 4 pure functions + `_safe()` path jail |
| `mcp_server.py` | new — ~12 line FastMCP stdio wrapper |
| `triage.py` | add `triage_agentic()`, `_fallback()`, agentic prompt |
| `mockapp.py` | `emit()` also appends to `run.jsonl` |
| `realapp.py` | `--agentic` flag wiring |
| `requirements.txt` | add `mcp>=1.2` |
| `.gitignore` | add `run.jsonl` |
| `README.md` | MCP section, updated architecture and next steps |

## Verification

1. **Offline, no API, no server**: `python mcp_tools.py` self-check — `_safe()` rejects
   `../../etc/passwd` and absolute paths; `read_source` returns numbered lines; `search_code`
   finds a known symbol; `get_logs` filters a fixture JSONL; `line_history` returns a commit
   for a line of `triage.py`.
2. **Server handshake, no API**: start `mcp_server.py` over stdio, `list_tools()`, assert all
   four appear with correct schemas, call `read_source` through the session. Proves MCP wiring
   independently of Gemini.
3. **Live, one triage**: `python realapp.py --agentic`, pick the constructor fault. The check
   that matters — does it call `read_source` on `constructor.py` *and* something we did not
   push into the prompt? Print every tool call so this is visible.
4. **The real test of whether pull beats push**: the composer fault deletes an alias guard.
   Push-mode has consistently blamed the *input document*. An agentic run can read the
   surrounding function and see the missing guard. If it still blames the input, MCP bought
   nothing here and that's the honest finding to record.
5. Budget: expect ≤5 requests per agentic triage. Verify against the printed call count.

## Risks

- **Quota.** One agentic triage ≈ 5 requests of 20/day. Test on `gemini-3.1-flash-lite`,
  save the stronger model for a confirming run.
- **15 RPM ceiling** is separate from the daily cap and a tool loop fires requests back to
  back. Existing `HttpRetryOptions` backoff should absorb it; this run will finally exercise
  that untested path.
- **The model may not call `submit_verdict`.** Handled by `_fallback`, and it escalates to
  human review rather than failing silently.
