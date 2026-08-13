# Tests reserved for the API key

Everything below needs `GEMINI_API_KEY` and spends against the free tier
(20 req/day, 15 req/min, both per model). Held until the work machine so the
personal key isn't burned mid-refactor. Run cheapest-and-riskiest first.

## A. Regression smoke (cheap, previously passed — confirm nothing broke)

| Test | Cost | Why | Look for |
|---|---|---|---|
| `python run.py check` | 1 req | `response_schema` structured output still parses | `resp.parsed` is a `TriageVerdict`, no exception |
| Force a bad model name / 404 | 1 req | `APIError` path still escalates instead of crashing | prints `⚠️ API 404`, returns `confidence=0.0`, `needs_human_review=True` |
| `python run.py mock --pick 1`, then grade it | 2 req | judge still scores `cause_*`/`fix_*` independently | both axes populated, `reasoning` names something concrete |
| `python run.py real --pick 2` (push mode) | 1 req | source-context prompt still changes the diagnosis | names `constructor.py` / `bool_values`, not a generic guess |

## B. Agentic / MCP path (previously verified 2026-08-12 — confirm still live)

| Test | Cost | Why | Look for |
|---|---|---|---|
| `python run.py real --agentic --pick 2` | ~4 req | full stdio handshake + tool loop still works end to end | `_print_tool_calls` shows `read_source`/`search_code` calls, then a submitted verdict naming line 238 and `.upper()`→`.lower()` |
| Same run, compare fix quality to push-mode's answer | included above | pull's remedy should still beat push's ("fix the YAML input" was push's wrong answer) | `proposed_fix` reverts the actual line, not the caller's input |
| Set `TRIAGE_TOOL_BUDGET=1`, rerun | ~2 req | `_fallback()` still escalates cleanly when the budget runs out before `submit_verdict` | prints `⚠️ no submit_verdict call`, `needs_human_review=True`, no crash |
| Path-jail-through-protocol check | 0 extra (watch existing run) | confirm the model never gets a tool result for a path outside `ROOT` | if the model ever tries `../..`, the tool call returns the `ValueError` string, not a file |

## C. Never yet run live — the actual open items

| Test | Cost | Why | Look for |
|---|---|---|---|
| **`HttpRetryOptions` recovery** — force a 429 or 503 with quota still available (e.g. burst several calls back to back) | variable, wasteful by design | never once observed recovering a request; only killed by exhausting quota during backoff | a request that 429s and then succeeds on its own, without a manual retry |
| **Noise experiment baseline** — `python run.py real --agentic --logged 1 --injected 1 --seed 0` | ~4 req | control: only the real bug on disk | correct diagnosis + correct fix, same as section B |
| **Noise experiment, decoys** — `run.py real --agentic --logged 1 --injected 9 --seed 0` | ~4-6 req | does 8 unreported bugs (2 in the same file as the real one) degrade the *fix* while the *cause* survives — this is Plan.md's core hypothesis, currently n=1 from an earlier ad hoc run | cause score should stay high; watch specifically whether the model reports a distractor in `constructor.py` instead of the real fault |
| **Noise experiment, pristine control** — `run.py real --agentic --logged 1 --injected 0 --seed 0` | ~4 req | can it diagnose with zero visible bug on disk (source reverted before triage) | expect low confidence / honest "cannot locate a defect", not a confident wrong guess |
| **Seed sweep** — repeat the decoy condition at `--seed 1..N` | ~4-6 req × N | one seed is an anecdote, not a result; this is what would turn the noise experiment into an actual finding | fix-score distribution across seeds — does the earlier 1.00→0.20 split replicate or was it luck |
| **Stronger model confirming pass** — same fault, `TRIAGE_MODEL=gemini-3.6-flash python run.py real ...` | ~4 req | Plan.md's suggestion to spread cost across two models rather than one day's quota | does the stronger model resist the same decoys that tripped the lite model |

## Notes

- Every run is now recorded to `runs.db` with its condition, so a live test's
  result is queryable afterwards with `python run.py runs` rather than scraped
  from scrollback. Replay any of them with `python run.py export --run <id>`.

- Sections A+B alone are ~10-12 requests — under half a day's quota, good as a
  first smoke pass on the new machine.
- Section C's noise experiment (baseline + decoy + pristine, one seed) is
  ~12-14 requests on its own — close to a full day. A seed sweep needs
  multiple days or splitting across `gemini-3.1-flash-lite` /
  `gemini-flash-latest` / `gemini-3.6-flash` (three independent 20/day
  buckets).
- None of section C has ground truth for the *distractor* claims — DISTRACTORS
  entries carry no `true_cause` by design, so grading there is eyeballed from
  `root_cause`/`proposed_fix` text, not the judge.
