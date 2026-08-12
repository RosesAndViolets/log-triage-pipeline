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
import subprocess
import sys
from pathlib import Path

import mockapp
import triage
from mockapp import BUFFER, TRUTH, EvalPipeline, PipelineHandler, new_trace

TARGET = Path(__file__).parent / "targets" / "pyyaml"
LIB = TARGET / "lib"
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


def select(logged: int, injected: int, seed: int) -> tuple[list[dict], list[dict]]:
    """Choose which faults are reported (Y) and which are merely present (X).

    A fault that produced a log is by definition in the code, so the logged set is
    always a subset of the injected set. Remaining slots are filled with real faults
    that stayed silent first, then distractors — silent real bugs are the better
    decoys, because they are the same kind of thing as the answer.
    """
    rng = random.Random(seed)
    pool = list(FAULTS)
    rng.shuffle(pool)
    chosen = pool[:logged]
    rest = pool[logged:] + [d for d in DISTRACTORS]
    rng.shuffle(rest)
    on_disk = chosen + rest[: max(0, injected - len(chosen))]
    return chosen, on_disk[:injected]


def apply(fault: dict):
    p = TARGET / fault["file"]
    src = p.read_text()
    if fault["old"] not in src:
        raise SystemExit(
            f"injection point not found in {fault['file']} — the clone has moved on.\n"
            f"Revert with: git -C {TARGET} checkout -- ."
        )
    p.write_text(src.replace(fault["old"], fault["new"], 1))


def revert():
    subprocess.run(["git", "-C", str(TARGET), "checkout", "--", "."], check=True)


def fresh_yaml():
    """Re-import the library from disk so the current injection is actually loaded."""
    for name in [m for m in sys.modules if m == "yaml" or m.startswith("yaml.")]:
        del sys.modules[name]
    import yaml

    assert "targets/" in yaml.__file__, f"loaded the wrong pyyaml: {yaml.__file__}"
    return yaml


def render_source(stack: str) -> str:
    """Source lines around each traceback frame that lives inside the target repo.

    Captured when the error is logged, not when it is triaged: by triage time the
    injection has been reverted and the file on disk no longer shows the bug.
    """
    blocks = []
    for path, lineno, func in FRAME_RE.findall(stack):
        if "targets/" not in path:  # our own harness frames aren't the suspect
            continue
        try:
            lines = Path(path).read_text().splitlines()
        except OSError:
            continue
        n = int(lineno)
        lo, hi = max(0, n - 1 - SOURCE_SPAN), min(len(lines), n + SOURCE_SPAN)
        body = "\n".join(
            f"  {'>' if i + 1 == n else ' '} {i + 1:>5} {lines[i]}" for i in range(lo, hi)
        )
        blocks.append(f"targets/{path.split('targets/', 1)[1]}:{n} in {func}\n{body}")
    return "\n\n".join(blocks[-3:])  # deepest 3 frames: where it actually broke


class SourcePipeline(EvalPipeline):
    """Adds the real source behind the traceback to the LLM's context."""

    def _fetch_context(self, log: dict) -> str:
        source = log["error"].get("source", "")
        trail = super()._fetch_context(log)
        return f"{source}\n\nlog trail:\n{trail}" if source else trail


def exercise(pipeline, times: int = 3, faults: list[dict] | None = None):
    """Run the broken library for real and log whatever it raises.

    One fault at a time, reverted before the next: a global fault like the scanner
    injection breaks parsing outright and masks every fault downstream of it.
    """
    handler = PipelineHandler(pipeline)
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
        finally:
            revert()


def _args() -> tuple[int, int, int]:
    """--logged Y, --injected X, --seed N. Defaults reproduce the previous behaviour."""
    def opt(name, default):
        return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default

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
    # Distractors rot like the real injection points do — fail loudly, not silently.
    for d in DISTRACTORS:
        src = (TARGET / d["file"]).read_text()
        assert src.count(d["old"]) == 1, f"distractor site not unique: {d['file']} {d['old']!r}"

    # Selection: logged is always a subset of injected, and seeds are reproducible.
    a_chosen, a_disk = select(1, 9, seed=7)
    b_chosen, b_disk = select(1, 9, seed=7)
    assert [f["service"] for f in a_chosen] == [f["service"] for f in b_chosen], "seed not stable"
    assert len(a_disk) == 9 and all(c in a_disk for c in a_chosen), "logged not inside injected"
    seeds = {select(1, 3, s)[0][0]["service"] for s in range(12)}
    assert len(seeds) > 1, f"seed changes nothing: {seeds}"
    assert select(1, 0, 0)[1] == [], "--injected 0 must leave the clone pristine"

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

    # Faults must be isolated: identical exception classes everywhere means one
    # fault is masking the others (the scanner injection does exactly that).
    classes = sorted(p.samples[fp]["error"]["class"] for fp in p.counts)
    assert len(set(classes)) > 1, f"faults are masking each other: {classes}"
    print(f"self-check ok — 3 isolated faults in real library code, "
          f"source context wired ({', '.join(classes)})")


if __name__ == "__main__":
    if not LIB.exists():
        raise SystemExit(
            f"missing clone. Run:\n  git clone --depth 1 "
            f"https://github.com/yaml/pyyaml {TARGET}"
        )
    sys.path.insert(0, str(LIB))
    try:
        _self_check()

        logged, injected, seed = _args()
        chosen, on_disk = select(logged, injected, seed)

        mockapp.reset()
        pipeline = SourcePipeline(threshold=3)
        exercise(pipeline, faults=chosen)

        # Put the injections back for the triage session. exercise() reverts each fault
        # as it goes, so by now the files are pristine — and read_source reads live disk,
        # unlike the push-mode snapshot taken at log time. Without this the model would
        # be handed correct code and asked why it crashed. Nothing executes from here on,
        # so the masking that forced isolation during exercise() cannot bite.
        for f in on_disk:
            apply(f)

        silent = len(on_disk) - len(chosen)
        print(f"\ncondition: {len(chosen)} logged / {len(on_disk)} injected / seed {seed}"
              f"   ({silent} unreported bug{'s' if silent != 1 else ''} in the code)")
        print(f"reported:  {', '.join(f['service'] for f in chosen) or 'none'}")
        print(f"{len(BUFFER)} log records from {TARGET.name}")
        pipeline.run(interactive=True, agentic="--agentic" in sys.argv)
    finally:
        revert()  # ponytail: leaves the clone clean even on Ctrl-C
        print("clone reverted to pristine.")
