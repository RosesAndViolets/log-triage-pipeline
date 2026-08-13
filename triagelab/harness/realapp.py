"""Inject faults into a real cloned codebase (PyYAML), exercise it, triage the fallout.

Unlike mockapp.py, the failures here happen inside somebody else's real code, so the
traceback points at genuine library frames — and the pipeline gets the actual source
lines around each frame as context.

Setup (once):
    git clone --depth 1 https://github.com/yaml/pyyaml targets/pyyaml

Run:
    python realapp.py            # faults are applied, then reverted on exit
"""

import logging
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

from triagelab.harness import mockapp
from triagelab.core import store
from triagelab.core import triage
from triagelab.harness.mockapp import BUFFER, TRUTH, EvalPipeline, PipelineHandler, new_trace

TARGET = Path(__file__).resolve().parents[2] / "targets" / "pyyaml"  # repo root
LIB = TARGET / "lib"
TRUTH_FILES: dict[str, str] = {}  # fingerprint -> the file the fault lives in
FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
SOURCE_SPAN = 4  # lines of source shown either side of a failing line

# Each fault is a real edit to real library code. `true_cause` describes what the
# edit actually did — the ground truth, never logged and never shown to the pipeline.
FAULTS = [
    {
        "service": "config-loader",
        "file": "lib/yaml/composer.py",
        "old": """            if anchor not in self.anchors:
                raise ComposerError(None, None, "found undefined alias %r"
                        % anchor, event.start_mark)
            return self.anchors[anchor]""",
        "new": "            return self.anchors[anchor]",
        "doc": "base: &tpl\n  retries: 3\nprod: *missing\n",
        "true_cause": (
            "The guard in Composer.compose_node that checks whether an alias is defined "
            "before looking it up was deleted, so an undefined YAML alias indexes the "
            "anchors dict directly and raises a bare KeyError instead of a ComposerError."
        ),
    },
    {
        "service": "feature-flags",
        "file": "lib/yaml/constructor.py",
        "old": "        return self.bool_values[value.lower()]",
        "new": "        return self.bool_values[value.upper()]",
        "doc": "dark_mode: true\nbeta_ui: false\n",
        "true_cause": (
            "SafeConstructor.construct_yaml_bool was changed to upper-case the scalar "
            "before looking it up in bool_values, whose keys are all lower-case, so every "
            "boolean in a document raises KeyError."
        ),
    },
    {
        "service": "manifest-parser",
        "file": "lib/yaml/scanner.py",
        "old": """            self.indents.append(self.indent)
            self.indent = column""",
        "new": """            self.indents.append(self.indent)
            self.indent = column + 1""",
        "doc": "service:\n  ports:\n    - 8080\n    - 9090\n  replicas: 2\n",
        "true_cause": (
            "Scanner.add_indent records the new indentation level as column + 1, one "
            "column deeper than the block actually starts, so nested block structures "
            "fail to close and the parser sees malformed input."
        ),
    },
]


# Bugs that are never exercised and never logged — the ninety flaws nobody filed a
# ticket for. They exist only to be found by a model reading the code, so they carry no
# `true_cause`: there is no right answer that names one of these.
#
# Safe because nothing executes after exercise(); the triage phase only reads files.
# A distractor cannot break a run the way the scanner fault did — it can only mislead.
# Two are in constructor.py on purpose: the adversarial case is a decoy the model trips
# over while reading the correct file.
DISTRACTORS = [
    {"service": "-", "file": "lib/yaml/reader.py",
     "old": "self.column += 1", "new": "self.column += 2"},
    {"service": "-", "file": "lib/yaml/reader.py",
     "old": "self.line += 1", "new": "self.line -= 1"},
    {"service": "-", "file": "lib/yaml/scanner.py",
     "old": "return self.tokens.pop(0)", "new": "return self.tokens.pop()"},
    {"service": "-", "file": "lib/yaml/scanner.py",
     "old": "if ch == '|' and not self.flow_level:", "new": "if ch == '|' and self.flow_level:"},
    {"service": "-", "file": "lib/yaml/constructor.py",
     "old": "return sign*int(value[2:], 16)", "new": "return sign*int(value[2:], 8)"},
    {"service": "-", "file": "lib/yaml/constructor.py",
     "old": "return sign*int(value, 8)", "new": "return sign*int(value, 10)"},
]


def select(logged: int, injected: int, seed: int,
           fault: str | None = None) -> tuple[list[dict], list[dict]]:
    """Choose which faults are reported (Y) and which are merely present (X).

    A fault that produced a log is by definition in the code, so the logged set is
    always a subset of the injected set. Remaining slots are filled with real faults
    that stayed silent first, then distractors — silent real bugs are the better
    decoys, because they are the same kind of thing as the answer.

    `fault` names a specific fault to log instead of drawing by seed — how the
    UI says "this run is about the feature-flags bug". Decoy fill is unchanged.
    """
    rng = random.Random(seed)
    pool = list(FAULTS)
    rng.shuffle(pool)
    if fault is not None:
        named = [f for f in pool if f["service"] == fault]
        if not named:
            raise SystemExit(f"--fault {fault!r}: no such fault. "
                             f"One of: {', '.join(f['service'] for f in FAULTS)}")
        pool = named + [f for f in pool if f["service"] != fault]
        logged = max(logged, 1)   # naming a fault means logging it
    chosen = pool[:logged]
    rest = pool[logged:] + [d for d in DISTRACTORS]
    rng.shuffle(rest)
    on_disk = chosen + rest[: max(0, injected - len(chosen))]
    return chosen, on_disk[:injected]


def apply(fault: dict):
    p = TARGET / fault["file"]
    src = p.read_text(encoding="utf-8")
    if fault["old"] not in src:
        raise SystemExit(
            f"injection point not found in {fault['file']} — the clone has moved on.\n"
            f"Revert with: git -C {TARGET} checkout -- ."
        )
    p.write_text(src.replace(fault["old"], fault["new"], 1), encoding="utf-8")


def revert():
    subprocess.run(["git", "-C", str(TARGET), "checkout", "--", "."], check=True)


def in_target(path: str) -> bool:
    """Is this path inside the cloned target?

    Compared as a Path, not a string: Windows tracebacks carry backslashes, so
    `"targets/" in path` is False there and every frame looks like ours.
    """
    try:
        Path(path).resolve().relative_to(TARGET.resolve())
        return True
    except (ValueError, OSError):
        return False


def rel_to_target(path: str) -> str:
    """Repo-relative, forward-slashed — stable in output on either platform."""
    return "targets/" + Path(path).resolve().relative_to(TARGET.resolve()).as_posix()


def fresh_yaml():
    """Re-import the library from disk so the current injection is actually loaded.

    The __pycache__ purge is load-bearing: pyc validity is source mtime (whole
    seconds) + size, and the boolean fault is a same-size edit — rewrite the
    file within the same second and the stale pyc of the *pristine* module
    loads, so the fault silently never fires. git checkout does not remove
    untracked __pycache__, which is how the staleness survives across runs.
    """
    for pyc in LIB.rglob("__pycache__"):
        shutil.rmtree(pyc, ignore_errors=True)
    for name in [m for m in sys.modules if m == "yaml" or m.startswith("yaml.")]:
        del sys.modules[name]
    import yaml

    assert in_target(yaml.__file__), f"loaded the wrong pyyaml: {yaml.__file__}"
    return yaml


def render_source(stack: str) -> str:
    """Source lines around each traceback frame that lives inside the target repo.

    Captured when the error is logged, not when it is triaged: by triage time the
    injection has been reverted and the file on disk no longer shows the bug.
    """
    blocks = []
    for path, lineno, func in FRAME_RE.findall(stack):
        if not in_target(path):  # our own harness frames aren't the suspect
            continue
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        n = int(lineno)
        lo, hi = max(0, n - 1 - SOURCE_SPAN), min(len(lines), n + SOURCE_SPAN)
        body = "\n".join(
            f"  {'>' if i + 1 == n else ' '} {i + 1:>5} {lines[i]}" for i in range(lo, hi)
        )
        blocks.append(f"{rel_to_target(path)}:{n} in {func}\n{body}")
    return "\n\n".join(blocks[-3:])  # deepest 3 frames: where it actually broke


class SourcePipeline(EvalPipeline):
    """Adds the real source behind the traceback to the LLM's context."""

    # The agent investigates the target codebase, never the harness: FAULTS
    # below holds the injection diffs and the true causes, and an unjailed
    # search_code was observed returning it to the model mid-run.
    tool_jail = "targets"

    def _fetch_context(self, log: dict) -> str:
        source = log["error"].get("source", "")
        trail = super()._fetch_context(log)   # sets log_lines/traces in the meta
        files = sorted(set(re.findall(r"^(\S+?):\d+ in ", source, re.M)))
        self.last_context_meta = {**self.last_context_meta, "files": files}
        return f"{source}\n\nlog trail:\n{trail}" if source else trail

    def truth_file(self, log: dict) -> str | None:
        """Which file the injected fault lives in — the retrieval ground truth.

        Read by the triage.judge node and nowhere earlier, same rule as TRUTH.
        """
        return TRUTH_FILES.get(triage.fingerprint(log))


def exercise(pipeline, times: int = 3, faults: list[dict] | None = None):
    """Run the broken library for real and log whatever it raises.

    One fault at a time, reverted before the next: a global fault like the scanner
    injection breaks parsing outright and masks every fault downstream of it.
    """
    handler = PipelineHandler(pipeline)
    TRUTH_FILES.clear()
    for f in faults if faults is not None else FAULTS:
        apply(f)
        try:
            yaml = fresh_yaml()
            log = logging.getLogger(f["service"])
            log.setLevel(logging.INFO)
            log.propagate = False
            log.handlers = [handler]
            for _ in range(times):
                trace = new_trace()
                log.info("parsing configuration document", extra={"trace_id": trace})
                try:
                    yaml.safe_load(f["doc"])
                except Exception as e:
                    log.error(
                        f"{type(e).__name__}: {e}", exc_info=True, extra={"trace_id": trace}
                    )
                    entry = BUFFER[-1]
                    entry["error"]["source"] = render_source(entry["error"]["stack"])
                    TRUTH[triage.fingerprint(entry)] = f["true_cause"]
                    TRUTH_FILES[triage.fingerprint(entry)] = f["file"]
        finally:
            revert()


def _args(argv=None) -> tuple[int, int, int]:
    """--logged Y, --injected X, --seed N. Defaults reproduce the previous behaviour."""
    argv = sys.argv if argv is None else argv

    def opt(name, default):
        return int(argv[argv.index(name) + 1]) if name in argv else default

    logged = opt("--logged", len(FAULTS))
    injected = opt("--injected", logged)
    if logged > len(FAULTS):
        raise SystemExit(f"--logged max is {len(FAULTS)}: only these carry ground truth")
    if injected > len(FAULTS) + len(DISTRACTORS):
        raise SystemExit(f"--injected max is {len(FAULTS) + len(DISTRACTORS)}")
    if injected and injected < logged:
        raise SystemExit("--injected cannot be below --logged: a bug that logged is in the code")
    return logged, injected, opt("--seed", 0)


def _self_check():
    # Self-sufficient about the clone: callable as `import realapp; _self_check()`
    # without the caller knowing it needs targets/pyyaml/lib on the path.
    if not LIB.exists():
        print(f"skipped — no clone at {TARGET} (git clone --depth 1 "
              f"https://github.com/yaml/pyyaml targets/pyyaml)")
        return
    if str(LIB) not in sys.path:
        sys.path.insert(0, str(LIB))

    # Distractors rot like the real injection points do — fail loudly, not silently.
    for d in DISTRACTORS:
        src = (TARGET / d["file"]).read_text(encoding="utf-8")
        assert src.count(d["old"]) == 1, f"distractor site not unique: {d['file']} {d['old']!r}"

    # A same-size fault must survive a same-second rewrite. pyc validity is
    # source mtime (whole seconds) + size; the boolean fault changes neither,
    # so without fresh_yaml's __pycache__ purge the pristine module loads and
    # the fault silently never fires — seen live as a run whose feature-flags
    # service logged three INFO lines and no error at all.
    import os
    f_neutral = next(f for f in FAULTS if f["service"] == "feature-flags")
    site = TARGET / f_neutral["file"]
    before = site.stat()
    fresh_yaml()                       # compile pycs for the pristine tree
    try:
        apply(f_neutral)
        os.utime(site, (before.st_atime, before.st_mtime))   # same second, same size
        y = fresh_yaml()
        try:
            y.safe_load(f_neutral["doc"])
            raised = False
        except Exception:
            raised = True
    finally:
        revert()
    assert raised, "stale __pycache__ hid a size-neutral fault"

    # Selection: logged is always a subset of injected, and seeds are reproducible.
    a_chosen, a_disk = select(1, 9, seed=7)
    b_chosen, b_disk = select(1, 9, seed=7)
    assert [f["service"] for f in a_chosen] == [f["service"] for f in b_chosen], "seed not stable"
    assert len(a_disk) == 9 and all(c in a_disk for c in a_chosen), "logged not inside injected"
    seeds = {select(1, 3, s)[0][0]["service"] for s in range(12)}
    assert len(seeds) > 1, f"seed changes nothing: {seeds}"
    assert select(1, 0, 0)[1] == [], "--injected 0 must leave the clone pristine"

    # A named fault must be the logged one regardless of seed, decoys unchanged.
    for s in range(3):
        f_chosen, f_disk = select(1, 3, s, fault="manifest-parser")
        assert f_chosen[0]["service"] == "manifest-parser", f_chosen
        assert f_chosen[0] in f_disk and len(f_disk) == 3, f_disk
    try:
        select(1, 3, 0, fault="no-such-service")
        raise AssertionError("unknown --fault was accepted")
    except SystemExit:
        pass

    mockapp.reset()
    p = SourcePipeline(threshold=3)
    exercise(p)

    assert len(p.counts) == 3, p.counts  # one signature per injected fault
    assert all(n >= 3 for n in p.counts.values()), p.counts
    assert all(fp in TRUTH for fp in p.counts), "a fault produced no ground truth"

    # Context must carry real library source, including the injected line itself.
    fp = next(fp for fp in p.counts if p.samples[fp]["serviceName"] == "feature-flags")
    ctx = p._fetch_context(p.samples[fp])
    assert "constructor.py" in ctx, ctx[:400]
    assert "bool_values[value.upper()]" in ctx, ctx[:400]  # the injected line itself

    # The real harness always jails the agent's code tools to the target.
    assert SourcePipeline.tool_jail == "targets", SourcePipeline.tool_jail

    # Retrieval must be evaluatable, not just present: the meta names the files,
    # and the fault's own file is the ground truth the judge node checks against.
    tf = p.truth_file(p.samples[fp])
    assert tf == "lib/yaml/constructor.py", tf
    files = p.last_context_meta["files"]
    assert any(f.endswith(tf) for f in files), files
    assert p.last_context_meta["log_lines"] > 0, p.last_context_meta

    # End to end, no API key: a stub run must land a component event on the
    # chain saying whether retrieval reached the faulty file. In a temp store —
    # a self-check must not leave its scaffolding in the real run picker.
    import shutil as _sh
    import tempfile
    real_dbs = (store.LOGS_DB, store.RUNS_DB)
    tmp = Path(tempfile.mkdtemp())
    store.LOGS_DB, store.RUNS_DB = tmp / "l.db", tmp / "r.db"
    try:
        mockapp.reset()
        with store.open_run(model="test", mode="stub", run_id="rc1") as (rid, chain):
            p2 = SourcePipeline(threshold=3).bind(rid, chain)
            p2.client = None
            exercise(p2)
            p2.run(interactive=False, agentic=False, pick=1)
        ev = store.load_run("rc1")["events"]
        comp = [e for e in ev if e["node"] == "triage.judge" and e["kind"] == "component"]
        assert comp, "no component event — retrieval was never evaluated"
        cp = comp[0]["payload"]
        assert cp["context_hit"] is True, cp  # its own traceback names its own file
    finally:
        store.LOGS_DB, store.RUNS_DB = real_dbs
        _sh.rmtree(tmp, ignore_errors=True)

    # Faults must be isolated: identical exception classes everywhere means one
    # fault is masking the others (the scanner injection does exactly that).
    classes = sorted(p.samples[fp]["error"]["class"] for fp in p.counts)
    assert len(set(classes)) > 1, f"faults are masking each other: {classes}"
    print(f"self-check ok — 3 isolated faults in real library code, "
          f"source context wired ({', '.join(classes)})")


def main(argv: list[str]):
    """Entry point for `run.py real` and for the server's subprocess launch."""
    if not LIB.exists():
        raise SystemExit(
            f"missing clone. Run:\n  git clone --depth 1 "
            f"https://github.com/yaml/pyyaml {TARGET}"
        )
    sys.path.insert(0, str(LIB))

    def opt(name, default=None):
        return argv[argv.index(name) + 1] if name in argv else default

    try:
        logged, injected, seed = _args(argv)
        agentic = "--agentic" in argv
        raw_pick = opt("--pick", "")
        pick = (int(raw_pick) if raw_pick.isdigit() else raw_pick) or None
        fault = opt("--fault")   # inject a *named* fault instead of a seeded draw
        if fault:
            # Naming a fault means the run is about it: log only it unless the
            # experiment says otherwise, and triage it without a separate --pick.
            if "--logged" not in argv:
                logged = 1
                if "--injected" not in argv:
                    injected = 1
            if pick is None:
                pick = fault
        chosen, on_disk = select(logged, injected, seed, fault=fault)
        disabled = frozenset(a for a in opt("--disable", "").split(",") if a)
        # The server picks the id up front so it can poll before we open the run.
        run_id_in = opt("--run-id", "")

        mockapp.reset()
        # The condition is recorded with the run, so a noise-experiment result
        # can never be read apart from the conditions that produced it.
        with store.open_run(
            model=triage.MODEL, mode="agentic" if agentic else "push", run_id=run_id_in,
            condition={"harness": "real", "logged": logged, "injected": injected,
                       "seed": seed, "pick": pick, "fault": fault,
                       "disabled": sorted(disabled),
                       "reported": [f["service"] for f in chosen],
                       # Which decoys were actually on disk — derivable from the
                       # seed, but evidence should say it outright — and which
                       # jail the code tools ran under. File alone is ambiguous
                       # (two distractors share constructor.py), so name the edit.
                       "on_disk": [f"{f['file']} · {f['old'].strip()[:48]}"
                                   for f in on_disk],
                       "jail": SourcePipeline.tool_jail},
        ) as (run_id, chain):
            pipeline = SourcePipeline(threshold=3).bind(run_id, chain, disabled)
            exercise(pipeline, faults=chosen)

            # Put the injections back for the triage session. exercise() reverts each
            # fault as it goes, so by now the files are pristine — and read_source reads
            # live disk, unlike the push-mode snapshot taken at log time. Without this the
            # model would be handed correct code and asked why it crashed. Nothing
            # executes from here on, so the masking that forced isolation cannot bite.
            for f in on_disk:
                apply(f)

            silent = len(on_disk) - len(chosen)
            print(f"\ncondition: {len(chosen)} logged / {len(on_disk)} injected / seed {seed}"
                  f"   ({silent} unreported bug{'s' if silent != 1 else ''} in the code)")
            print(f"reported:  {', '.join(f['service'] for f in chosen) or 'none'}")
            print(f"{len(BUFFER)} log records from {TARGET.name}")
            pipeline.run(interactive=pick is None, agentic=agentic, pick=pick)
            mockapp._report(pipeline)
            print(f"\nrun {run_id} recorded — replay with:"
                  f"\n  python run.py export --run {run_id}")
        return run_id
    finally:
        revert()  # ponytail: leaves the clone clean even on Ctrl-C
        print("clone reverted to pristine.")


if __name__ == "__main__":
    store.use_utf8_stdout()
    if "--no-self-check" not in sys.argv:
        if not LIB.exists():
            raise SystemExit(f"missing clone: {TARGET}")
        sys.path.insert(0, str(LIB))
        _self_check()
    main(sys.argv[1:])
