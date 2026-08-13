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

## Start to finish

Everything goes through `run.py`. There is no other command to remember.

```bash
git clone git@github.com:RosesAndViolets/log-triage-pipeline.git
cd log-triage-pipeline
pip install -r requirements.txt

# The fault-injection target: someone else's real repo, not vendored here.
git clone --depth 1 https://github.com/yaml/pyyaml targets/pyyaml

python run.py check          # 1. prove the wiring — no API key, no network
python run.py mock --pick 1  # 2. one run, one triage, recorded
python run.py runs           # 3. what got recorded
python run.py serve          # 4. drive it from a browser
```

**Step 1 costs nothing and needs no key.** Nine self-checks plus the portability
guard. If it passes, the pipeline is wired correctly on this machine — that is
the first thing to run on a new laptop, and the first thing to run when something
looks wrong.

**Step 2 spends exactly one request.** Without `--pick` it shows a numbered menu
and pressing `q` spends nothing. For a real key:

```bash
export GEMINI_API_KEY="your-key"     # from https://aistudio.google.com/apikey
```

A real key is 39 characters and starts with `AIza`. Put the export in `~/.zshenv`
(not `~/.zshrc`) so non-interactive shells — VS Code's Run button, subprocesses —
see it too. On Windows: `set GEMINI_API_KEY=...`, or
`$env:GEMINI_API_KEY="..."` in PowerShell.

### The commands

| Command | What it does |
|---|---|
| `run.py check` | Every self-check plus portability. No key, no network, no quota. |
| `run.py mock` | Synthetic faults: five services failing in realistic ways, graded against known causes. **Start here.** |
| `run.py real` | Injects real bugs into the cloned PyYAML, runs it, triages the actual tracebacks. |
| `run.py runs` | Every recorded run, with whether its chain still verifies. |
| `run.py export` | One run → a standalone HTML replay you can send to somebody. |
| `run.py serve` | The control surface: start runs, watch them, switch nodes off. |

Flags that apply to both harnesses:

| Flag | Means |
|---|---|
| `--agentic` | The model fetches its own context through the MCP tools, instead of being handed a packet. |
| `--pick N` | Triage the Nth signature over threshold, no prompt. Without it you get the menu. A service name works too: `--pick checkout-api`. |
| `--disable NODE,NODE` | Switch DAG nodes off for this run. Recorded with the run. |
| `--fault SERVICE` | `real` only — inject and triage that named fault instead of a seeded draw (e.g. `--fault feature-flags`). |
| `--logged Y --injected X --seed N` | `real` only — the noise experiment. See below. |

### Driving it from the browser

```bash
python run.py serve          # → http://127.0.0.1:8000
```

The page is the system map, live. You can:

- **start a run** — pick the harness, push or agentic, and which error it is about
  (in `mock` that chooses what gets triaged; in `real` it also chooses which fault
  is injected into the clone);
- **watch the chain fill in** as it happens, node by node, rather than after the fact;
- **switch nodes off** with the checkboxes, per run;
- **scrub back** through any earlier run from the dropdown.

Switching a node off is not cosmetic. `triage.judge` unchecked means the judge
never runs — no prompt, no second API call — and the chain records
`triage.judge skip "switched off"` while the run's `condition` remembers what was
disabled. A result can never be read apart from the configuration that produced it.

**The graph itself stays in `triagelab/core/nodes.py`.** The UI chooses which
declared nodes fire; it cannot describe a graph the code is unable to run, so the
chain always matches what actually happened. Adding or reordering a step is a code
edit, deliberately.

The server binds `127.0.0.1` and runs harnesses on request. It is a local
development tool and should not listen on anything else.

### Keeping a run

```bash
python run.py export --run 20260813T172501 --out map.html
```

One self-contained file — no server, no network, openable anywhere, publishable.
It states its own provenance: run id, mode, event count, and whether the chain
verified.

---

## Layout

```
run.py                  the only entry point
triagelab/
  core/       store.py    two databases + the evidence chain
              dag.py      the graph engine
              nodes.py    the pipeline, declared as a graph
              triage.py   dedup, threshold, the Gemini call, routing
  toolserver/ tools.py    the four tools the model can pull with
              server.py   the MCP stdio wrapper over them
  harness/    mockapp.py  synthetic faults + the judge
              realapp.py  real bugs injected into a real library
  viewer/     serve.py       the live control surface
              export_run.py  a run → a standalone page
              map_template.html
scripts/      check_portable.py
docs/         Plan.md  tests_with_api.md
data/         logs.db  runs.db          (gitignored)
targets/      the cloned PyYAML         (gitignored)
```

Every module is runnable on its own for its self-check —
`python -m triagelab.core.store` — which is what `run.py check` does for all of
them at once.

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

- **The cap is per model.** `TRIAGE_MODEL=gemini-3.6-flash python run.py mock` draws
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
python run.py real --agentic      # or: python run.py mock --agentic
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
are plain functions in `triagelab/toolserver/tools.py` — `python -m triagelab.toolserver.tools`
exercises all four, plus
the path jail, with no server and no API key.

## Broken vs reported: the noise experiment

In production the number of flaws in the code and the number that produced logs are not
the same number. If a hundred bugs exist and ten get reported, a model reading source
meets the other ninety — each a plausible culprit next to the real one.

`realapp.py` separates the two:

```bash
python run.py real --agentic --logged 1 --injected 9 --seed 1
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
| `--injected 1` | case mismatch — correct | revert line 238 to `.lower()` — correct | cause 1.00 / fix 1.00 |
| `--injected 9` | case mismatch — correct | add `'TRUE'`/`'FALSE'` to `bool_values` — **patches around the bug** | cause 1.00 / fix **0.20** |

(The fix column is scored as of 2026-08-13; both runs originally showed a flat 1.00,
which is what hid the difference.)

The diagnosis survived eight decoys. The *remedy* did not: under noise it treated the
injected `.upper()` as intended and proposed changing the dictionary to match it. The
grader now separates the two, so the run summary reports a **cause-minus-fix gap** — a
pipeline that diagnoses well and prescribes badly no longer looks perfect.

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

## The DAG, the state engine, and the evidence chain

The pipeline is a **declared graph**, not a call chain. `nodes.py` holds two DAGs —
ingestion runs once per log record, triage runs once per signature somebody chose to
spend a request on:

```
ingest.gate → ingest.normalize → ingest.fingerprint → ingest.count

triage.select → triage.context      ─┐
             └→ triage.investigate  ─┴→ triage.verdict → triage.route → triage.judge
```

The branch is the reason it is a graph: `triage.context` runs in push mode,
`triage.investigate` in agentic mode, and which one ran is data rather than an `if`
buried three frames down. `dag.py` walks it, and records every node's `enter`,
`exit`, `skip` and `error` without each node remembering to. A node returning `HALT`
ends that walk cleanly — an INFO line stopped at the gate is finished, not broken.

**Node bodies call the functions that already existed.** `normalize()`,
`fingerprint()`, `_fetch_context()`, `_agentic_call()` and `alert()` are unchanged
and still work when called directly. This is a change of orchestration, not of logic.

### Two databases, deliberately separate

| | holds | why separate |
|---|---|---|
| `logs.db` | ingested log records | input the pipeline **reads** |
| `runs.db` | `run` + `event` — the evidence chain | testimony **about** the pipeline |

Every log row carries a `run_id`, so `get_logs` is scoped to the current run. That is
what **removed the need to truncate logs between runs**: the old `run.jsonl` was
wiped each time only so a stale line could not be served for a fresh error. Deleting
history to keep a query honest was always the weaker fix — now the history is kept
and the query is correct.

### Why it is a chain and not a table

Each event carries the hash of the one before it:

```
hash = sha256(prev_hash | seq | node | kind | canonical_json(payload))
```

`store.verify(run_id)` recomputes the whole thing and returns the first `seq` that
fails, or `None`. An edited payload and a deleted row both break the link. This is
what lets a replay be **checked rather than trusted** — without it, "a replay of a
run" and "a drawing someone made" are the same artifact.

```
python run.py runs                          # every recorded run
python -m triagelab.core.store --verdicts   # the flat verdict/grade view
```

### Replaying a run

```
python run.py runs
python run.py export --run 20260813T172501 --out map.html
```

`triagelab/viewer/map_template.html` holds the node **positions**; `nodes.py` holds which nodes
**exist**. A DAG node with no layout entry fails `nodes.py`'s self-check rather than
quietly vanishing from the picture. The exported page states its own provenance —
run id, mode, event count, and whether the chain verified — so a replay that does not
match the run it claims to show says so on its face.

`verdicts.jsonl` and `run.jsonl` are **gone**. Both are superseded by the chain, and
carrying two records of the same fact is how records drift.

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

1. ~~The judge only grades `root_cause`~~ — **fixed 2026-08-13.** `JudgeScore` now
   carries `cause_correct`/`cause_score` and `fix_correct`/`fix_score`, graded
   independently, and the prompt names the specific failure to catch: a remedy that
   works around the defect, adapts other code to tolerate it, or tells the caller to
   change their input. Verified by replaying both historical verdicts for the same
   fault — the reverting fix scored `PASS 1.00`, the patch-around-it fix `FAIL 0.20`,
   with identical cause scores of 1.00.
2. **Counts reset every run.** `Counter` is in-memory; nothing accumulates across runs.
   Still open — each run re-ingests a fresh batch, so there is no genuine cross-run
   accumulation to preserve yet. `runs.db` is where it would live if there were.
3. ~~Verdicts are stdout-only~~ — **fixed 2026-08-13.** Superseded the same day by the
   evidence chain: verdicts and grades are `event` rows in `runs.db`, alongside every
   other step. The interim `verdicts.jsonl` was removed rather than kept in parallel.
   See *The DAG, the state engine, and the evidence chain* above.
4. ~~`search_code` bypassed the path jail~~ — **found and fixed 2026-08-13.**
   `read_source` and `line_history` both routed their argument through `_safe()`;
   `search_code` walked `ROOT.glob(glob)` directly. A literal `..` segment is a
   traversal step rather than a wildcard, so `search_code("x", "../*.py")` read and
   returned files **outside** the repo root — defeating the exact threat `_safe()`
   exists for, on one of the four tools the model can call. Every glob hit now goes
   through `_safe()`. The self-check builds a temp root with a sibling file one level
   up and asserts it stays invisible; reverting the fix makes that assertion fail.
5. **Injection points are string matches** against upstream PyYAML source. If upstream
   moves, `apply()` fails loudly with the revert command rather than silently injecting
   nothing — but they will eventually need updating.
6. **Fault masking is a live hazard.** Fixed by isolation; the `realapp.py` self-check
   asserts more than one distinct exception class specifically to catch a regression.

---

## Next steps

The MCP toolserver in [`Plan.md`](Plan.md) is **built and verified** — see *Pulling
context through MCP* above.

Everything below needing a live key is specified in
[`tests_with_api.md`](tests_with_api.md) — what to run, what it costs, and what to
look for — so it can be executed in one sitting on a machine with quota.

1. **Run the noise-experiment seed sweep.** The highest-value open question, and now
   measurable: the cause/fix split can see degradation, and `runs.db` holds every run's
   chain and condition across the several days the sweep will take. Currently n=1.
2. **Persist counts** with `shelve` if you ever feed this a live stream rather than a
   batch.
3. **Retry/backoff verification** — confirm `HttpRetryOptions` actually recovers a 429
   on a day with quota remaining. Never once observed.
4. **More fault classes** — concurrency and resource-exhaustion bugs are the ones where
   context fetching should pay off most, and they're not represented yet.

---

## Setup notes

`targets/` is gitignored — that's someone else's 3.5MB repo, not ours to version. It
must be cloned on each new machine (one command, in Quick start above).

**Moving by USB:** copy the whole folder. `targets/` comes along as plain files, so no
setup step is needed on the other side beyond `pip install -r requirements.txt` and the
API key. No source file contains an absolute path.

**Interpreter:** developed on conda env `yue`, Python 3.13.2. Any Python ≥3.10 works.

---

## Running this on Windows

The repo is meant to clone onto a Windows machine and work. `python run.py check`
guards that, and it is worth running before you push:

```
python run.py check
```

It fails on the four things that break silently on macOS and loudly on Windows.
Each was a real bug in this repo, not a hypothetical:

1. **Text I/O without `encoding=`.** Windows defaults to the locale code page
   (usually cp1252), so the non-ASCII this repo genuinely contains — em dashes in log
   messages, arrows in output — either mangles or raises. Every `open`, `read_text`
   and `write_text` now passes `encoding="utf-8"`; the checker walks the AST to prove
   it, and exempts binary mode.
2. **Paths compared as strings.** `"targets/" in path` is False on Windows, where
   tracebacks carry backslashes — so `realapp.py` would have treated every library
   frame as its own and captured no source at all. Now `in_target()` compares
   resolved `Path` objects, and `rel_to_target()` emits forward slashes for output
   only. The checker flags string membership tests against path-shaped names.
3. **Emoji to a cp1252 console.** The status lines (`🔎`, `✅`, `🚨`) raise
   `UnicodeEncodeError` on `cmd.exe`, killing a run on a decoration. Every entry
   point calls `store.use_utf8_stdout()` first. It is called from `__main__` blocks
   rather than at import, because rebinding another program's stdout because it
   imported you is rude.
4. **Unclosed SQLite connections.** Unix happily deletes an open file; Windows keeps
   a lock, so a leaked handle turns into a failure to reopen or delete the database.
   Every connection is closed through a context manager, and `store.py`'s self-check
   probes for live handles by trying to use them — a *closed* `Connection` is still a
   `Connection` object, so identity is not the test.

**Setup is the same three commands**, `py` instead of `python3` if that is your
launcher:

```
pip install -r requirements.txt
git clone --depth 1 https://github.com/yaml/pyyaml targets/pyyaml
set GEMINI_API_KEY=...          # PowerShell: $env:GEMINI_API_KEY="..."
```

`git` must be on `PATH` — `realapp.py` shells out to it to revert injected faults, and
`mcp_tools.line_history` uses it. Not verified on a real Windows machine yet; the
checker encodes what is known to differ, not everything that could.
