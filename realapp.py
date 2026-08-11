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
import re
import subprocess
import sys
from pathlib import Path

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


def exercise(pipeline, times: int = 3):
    """Run the broken library for real and log whatever it raises.

    One fault at a time, reverted before the next: a global fault like the scanner
    injection breaks parsing outright and masks every fault downstream of it.
    """
    handler = PipelineHandler(pipeline)
    for f in FAULTS:
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


def _self_check():
    BUFFER.clear()
    TRUTH.clear()
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

        BUFFER.clear()
        TRUTH.clear()
        pipeline = SourcePipeline(threshold=3)
        exercise(pipeline)
        print(f"\nexercised {len(FAULTS)} injected faults in {TARGET.name}, "
              f"{len(BUFFER)} log records")
        pipeline.run(interactive=True)
    finally:
        revert()  # ponytail: leaves the clone clean even on Ctrl-C
        print("clone reverted to pristine.")
