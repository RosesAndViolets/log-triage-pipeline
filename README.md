# Log Triage Pipeline

A prototype that watches a stream of error logs, deduplicates them into signatures,
waits until a signature is frequent enough to be worth attention, then asks Gemini to
diagnose it and routes the verdict like a Slack alert.

It also grades itself: the fault simulators know what they broke, and an LLM judge
scores the pipeline's diagnosis against that ground truth.

```
logs ─→ dedup gate ─→ normalization ─→ frequency threshold ─→ LLM triage ─→ routing
         (MD5 of        (scrub ids,      (only signatures      (Gemini +      (auto vs
      service+class      timestamps,      seen N+ times)        Pydantic      human
        +message)         numbers)                              schema)       review)
```

---

## Quick start

```bash
git clone git@github.com:RosesAndViolets/log-triage-pipeline.git
cd log-triage-pipeline
pip install -r requirements.txt

# Fault-injection target for realapp.py (not vendored — see Setup notes)
git clone --depth 1 https://github.com/yaml/pyyaml targets/pyyaml

export GEMINI_API_KEY="your-key"     # from https://aistudio.google.com/apikey
python mockapp.py
```

A real key is 39 characters and starts with `AIza`. Put the export in `~/.zshenv`
(not `~/.zshrc`) so non-interactive shells — VS Code's Run button, subprocesses —
also see it.

**Nothing is spent until you ask.** Both simulators show a menu; pressing `q`
triages nothing and makes zero API calls.

---

## The three entry points

| File | What it does | Run it? |
|---|---|---|
| `triage.py` | The pipeline itself: dedup, threshold, Gemini call, routing. Also has a small hand-written demo under `__main__`. | Library, mostly |
| `mockapp.py` | Synthetic fault simulator. Five services fail in realistic ways; graded against known causes. | **Start here** |
| `realapp.py` | Injects real bugs into a cloned PyYAML, runs it, triages the actual tracebacks. | The interesting one |

Both simulators run an offline `_self_check()` first that needs no API key and no
network. If that passes, the wiring is intact.

---

## Read this before you debug a 429

The Gemini free tier allows **20 requests per day, per model, per project**. Not per
minute — per day. The first time you hit it, it looks exactly like a broken pipeline.

Check what actually tripped:

```python
except errors.APIError as e:
    print(e.details)   # quotaId, quotaValue, retryDelay
```

Three things follow from this:

- **The cap is per model.** `TRIAGE_MODEL=gemini-3.6-flash python mockapp.py` draws
  on a completely separate 20 from the default `gemini-3.1-flash-lite`.
- **Picking is interactive** so you spend one call per triage, deliberately.
- **Grading is opt-in** per verdict (`grade this verdict? [y/N]`), because the judge
  is a second call. The ground-truth line prints for free either way.

Also: `gemini-2.5-flash` is retired for new API keys, but still appears in
`client.models.list()`. Only an actual `generateContent` call reveals it's blocked —
don't trust the list endpoint.

---

## Pulling context through MCP

By default the model gets a context packet chosen for it before the call. With
`--agentic` it fetches its own, through a stateless MCP server over stdio:

```bash
python realapp.py --agentic      # or: python mockapp.py --agentic
```

| Tool | What it answers |
|---|---|
| `read_source` | the actual code behind any frame in the traceback |
| `search_code` | where is this defined, who else calls it |
| `get_logs` | the lines around this error, by `trace_id` or service |
| `line_history` | when this line last changed, and in which commit |

The verdict comes back as a `submit_verdict` tool call, **not** via
`response_schema` — with tools set, the API returns a dangling `function_call` and
`parsed` is `None`. Pydantic validates the arguments on our side instead. If the
model never submits, `_fallback()` escalates to human review rather than guessing.

**This is the measured difference.** On the same injected PyYAML fault:

- *Push*: identified the case mismatch, then proposed fixing the **YAML input files**.
  The bug was in the library. Judge scored it 1.00 anyway.
- *Pull*: named `constructor.py`, line 238, `bool_values[value.upper()]`, and proposed
  reverting it to `.lower()` — the exact injection. It got there by calling
  `search_code("bool_values")` to find where the dict is defined, which no pushed
  context ever contained.

Three implementation details that are load-bearing:

- **`mcp` must be `<2`.** google-genai 2.17 reads `tool.inputSchema`; mcp 2.0 renamed it
  `input_schema`, and the SDK's adapter raises `AttributeError` against 2.x.
- **Pass `config` as a dict.** The `GenerateContentConfig` branch runs
  `model_copy(deep=True)`, and a live `ClientSession` holds an unpicklable
  `_asyncio.Task`.
- **Budget is `+1`.** `submit_verdict` counts against `maximum_remote_calls`.
  `TRIAGE_TOOL_BUDGET` (default 4) is the *investigation* budget; the verdict gets its
  own slot.

Cost: one agentic triage is up to 6 requests against a 20/day cap. The tools themselves
are plain functions in `mcp_tools.py` — `python mcp_tools.py` exercises all four, plus
the path jail, with no server and no API key.

## Broken vs reported: the noise experiment

In production the number of flaws in the code and the number that produced logs are not
the same number. If a hundred bugs exist and ten get reported, a model reading source
meets the other ninety — each a plausible culprit next to the real one.

`realapp.py` separates the two:

```bash
python realapp.py --agentic --logged 1 --injected 9 --seed 1
#                            │           │            └ same seed, same run, every time
#                            │           └ bugs present on disk while the model reads
#                            └ bugs actually exercised, which produced the error logs
```

- **`--logged Y`** — max 3, since only those three carry ground truth.
- **`--injected X`** — max 9: the 3 real faults plus 6 `DISTRACTORS`, one-line PyYAML bugs
  that are never run and have no right answer attached. Two live in `constructor.py`, so a
  decoy sits in the same file as the real boolean fault.
- **`X = 0`** is the pristine control: the errors were reported, but the code is clean by
  triage time. Can it diagnose what it cannot see?
- Default is `X = Y = 3`, unchanged from before.

Every run prints its condition, so a screenshot is self-describing:

```
condition: 1 logged / 9 injected / seed 1   (8 unreported bugs in the code)
```

This is safe because nothing executes after `exercise()` — the triage phase only reads
files. A distractor cannot break a run the way the scanner fault broke parsing; it can
only mislead a reader, which is the point.

### First observation, and why it is not yet a result

Same constructor fault, agentic, `gemini-3.1-flash-lite`:

| Condition | Root cause | Proposed fix | Judge |
|---|---|---|---|
| `--injected 1` | case mismatch — correct | revert line 238 to `.lower()` — correct | 1.00 |
| `--injected 9` | case mismatch — correct | add `'TRUE'`/`'FALSE'` to `bool_values` — **patches around the bug** | 1.00 |

The diagnosis survived eight decoys. The *remedy* did not: under noise it treated the
injected `.upper()` as intended and proposed changing the dictionary to match it. Both
scored 1.00, because the judge never reads `proposed_fix`.

**One run per condition, and the model is nondeterministic — this is a hypothesis, not a
finding.** Repeat across seeds before claiming the decoys caused it. That is what `--seed`
is for.

## Architecture

**Logs are produced by real failures.** Neither simulator writes log strings by hand.
They call functions that genuinely raise — a socket connect to a dead port, `json.loads`
on a truncated body, a missing import — and catch the result. So exception classes,
stack frames, and line numbers are all real.

```
your code raises
   └─→ logging.Logger
         └─→ PipelineHandler.emit()          (mockapp.py)
               ├─→ BUFFER   deque(maxlen=2000), every level — this is the context source
               └─→ pipeline.ingest()          ERROR/FATAL only → Counter of fingerprints
```

**Deduplication** hashes `serviceName + error.class + normalize(message)`. `normalize()`
scrubs timestamps, UUIDs and *all* digits, so `5031ms` and `5044ms` collapse to one
signature. (The digit regex deliberately has no `\b` — `\b\d+\b` misses `5031ms`,
which silently defeats the threshold.)

**Context fetching** is what makes hard cases diagnosable. `_fetch_context()` returns
the log lines sharing the error's `trace_id` plus the tail from that service. The
accumulation fault proves it works: its error message says only *"queue depth 1001
exceeded capacity 1000"*, yet the verdict correctly names **consumer lag** — a phrase
that exists nowhere except the INFO lines pulled from the buffer.

`realapp.py` extends this with **source code**: it walks the traceback, keeps frames
inside `targets/`, and includes the surrounding lines with the failing one marked.

**Ground truth** lives in `TRUTH[fingerprint] → true_cause`, written by the simulator
and never logged. The pipeline cannot see it. The judge compares the verdict against
it afterwards.

**`needs_human_review` is computed, not trusted.** A Pydantic `model_validator`
overwrites whatever the model returned with `confidence < 0.8`, because routing is
not the model's decision to make.

---

## How `realapp.py` injects faults

Three real bugs, each in a different PyYAML layer:

| Layer | Injection | Surfaces as |
|---|---|---|
| composer | delete the "is this alias defined?" guard | `KeyError` on an undefined alias |
| constructor | `bool_values[value.upper()]` against lower-case keys | `KeyError` on every boolean |
| scanner | record indent as `column + 1` | `ParserError` on nested blocks |

**Faults are applied one at a time and reverted between.** With all three live, the
scanner bug breaks parsing globally and masks the other two — every document died with
an identical `ParserError`. `sys.modules` is purged between faults so the re-import
actually picks up the edit on disk.

**Source is captured when the error is logged, not when it's triaged**, because the
injection is already reverted by triage time and the file on disk no longer shows the bug.

The clone is restored in a `finally`, so it stays pristine even on Ctrl-C. To reset
manually: `git -C targets/pyyaml checkout -- .`

---

## Status

**Verified live against the API:**
- End-to-end triage with Pydantic structured output (`response_schema` → `resp.parsed`)
- Context fetching demonstrably changing the diagnosis (the consumer-lag result)
- LLM judge scoring verdicts against ground truth
- `APIError` handling — a forced 404 escalated to human review instead of killing the run
- Interactive picker spending exactly one call per pick
- Source-level context on injected PyYAML faults

**Verified live 2026-08-12 (agentic path):**
- MCP stdio handshake, all four tools listed with correct schemas
- The path jail refusing `../../../etc/passwd` *through* the protocol, as a tool error
- A full agentic triage: 3 tool calls plus `submit_verdict`, judged 1.00
- `_fallback()` escalating when the model spent its budget without submitting — hit for
  real on the first run, before the `+1` slot was added

**Not verified:**
- `HttpRetryOptions` backoff. It's configured on the client but has never successfully
  recovered a 429 in a completed run — the run that would have proven it was killed
  after grinding through backoff for 10+ minutes on an exhausted daily quota.

---

## Known bugs and limitations

1. **The judge only grades `root_cause`, never `proposed_fix`.** This is not theoretical:
   on the PyYAML constructor fault the verdict correctly identified the `bool_values`
   case mismatch, then proposed *fixing the YAML input files* — when the actual bug was
   in the library code. It scored 1.00. Your grader is more lenient than it looks.
2. **Counts reset every run.** `Counter` is in-memory; nothing accumulates across runs.
   `shelve` would be the lazy fix.
3. **Verdicts are stdout-only.** Nothing is persisted, so you can't compare judge scores
   before and after a prompt change.
4. **Injection points are string matches** against upstream PyYAML source. If upstream
   moves, `apply()` fails loudly with the revert command rather than silently injecting
   nothing — but they will eventually need updating.
5. **Fault masking is a live hazard.** Fixed by isolation; the `realapp.py` self-check
   asserts more than one distinct exception class specifically to catch a regression.

---

## Next steps

The MCP toolserver in [`Plan.md`](Plan.md) is **built and verified** — see *Pulling
context through MCP* above.

1. **Grade `proposed_fix` too.** Highest value — it's the axis where the pipeline
   currently fails silently. Add a second field to `JudgeScore`.
2. **Persist verdicts** to `verdicts.csv` (~6 lines with `csv.writer`), so prompt and
   model changes can be compared instead of eyeballed.
3. **Persist counts** with `shelve` if you ever feed this a live stream rather than a
   batch.
4. **Retry/backoff verification** — confirm `HttpRetryOptions` actually recovers a 429
   on a day with quota remaining.
5. **More fault classes** — concurrency and resource-exhaustion bugs are the ones where
   context fetching should pay off most, and they're not represented yet.

---

## Setup notes

`targets/` is gitignored — that's someone else's 3.5MB repo, not ours to version. It
must be cloned on each new machine (one command, in Quick start above).

**Moving by USB:** copy the whole folder. `targets/` comes along as plain files, so no
setup step is needed on the other side beyond `pip install -r requirements.txt` and the
API key. No source file contains an absolute path.

**Interpreter:** developed on conda env `yue`, Python 3.13.2. Any Python ≥3.10 works.
