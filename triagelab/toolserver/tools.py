"""The triage toolset: what the model can pull for itself.

Plain functions, deliberately. MCP is a thin wrapper over this module
(`mcp_server.py`), so these can be tested — and called — with no server, no
protocol and no API key involved.

Everything here is stateless: each call takes what it needs as arguments and
reads from disk. Nothing is remembered between calls, so restarting the server
loses nothing.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from triagelab.core import store

# The repo root, NOT this package's directory. The jail has to cover targets/ —
# scoping it to triagelab/toolserver/ would leave every existing test passing while
# read_source silently lost the ability to see the code it exists to read.
ROOT = Path(__file__).resolve().parents[2]
MAX_SPAN = 200  # lines per read_source call — the model can ask again, tokens are the budget
GIT_TIMEOUT = 10  # seconds — a hung git process must not hang the whole tool loop


def _jail() -> Path:
    """Where tool calls are confined. Defaults to the repo root.

    TRIAGE_TOOL_JAIL narrows it below the root — the real harness sets it to
    "targets", because its FAULTS table holds the injection diffs and the
    ground-truth text, and search_code('bool_values') was observed live
    returning that answer sheet to the agent mid-run. Read per call so the
    server needs no restart to change rules between runs.
    """
    sub = os.getenv("TRIAGE_TOOL_JAIL", "")
    return (ROOT / sub).resolve() if sub else ROOT


def _safe(path: str) -> Path:
    """Resolve a model-supplied path, or refuse.

    This is a trust boundary: the argument is chosen by an LLM reasoning about
    a traceback, and a traceback contains absolute paths pointing anywhere on
    the machine. Symlinks are resolved before the check, so a link inside the
    repo cannot walk out of it.
    """
    p = (ROOT / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    if not p.is_relative_to(ROOT):
        raise ValueError(f"path escapes the repo root: {path}")
    jail = _jail()
    if jail != ROOT and not p.is_relative_to(jail):
        raise ValueError(f"path is outside this run's tool jail ({jail.name}/): {path}")
    return p


def read_source(path: str, start_line: int = 1, end_line: int = 0) -> str:
    """Read numbered source lines from a file in the repository.

    Args:
        path: File path, relative to the repo root (e.g. "targets/pyyaml/lib/yaml/scanner.py").
        start_line: First line to read, 1-indexed.
        end_line: Last line to read. 0 means "start_line + 60".
    """
    p = _safe(path)
    if not p.is_file():
        return f"no such file: {path}"
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(1, start_line)
    end = min(len(lines), end_line or start + 60)
    if end < start:
        return f"empty range: {start_line}..{end_line}"
    if end - start > MAX_SPAN:
        end = start + MAX_SPAN
    body = "\n".join(f"{i:>5} {lines[i - 1]}" for i in range(start, end + 1))
    return f"{path} lines {start}-{end} of {len(lines)}\n{body}"


def search_code(pattern: str, glob: str = "**/*.py", max_hits: int = 40) -> str:
    """Regex-search the repository for a pattern. Use this to find callers or definitions.

    Args:
        pattern: Python regular expression.
        glob: Which files to search (e.g. "targets/**/*.py").
        max_hits: Stop after this many matches.
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"bad regex: {e}"
    hits = []
    for raw in sorted(ROOT.glob(glob)):
        # A literal ".." path segment is not a wildcard, so glob() never runs it
        # through _safe() on its own — route every hit through the jail here,
        # same as read_source and line_history already do.
        try:
            p = _safe(str(raw))
        except ValueError:
            continue
        if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            for n, line in enumerate(
                    p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:160]}")
                    if len(hits) >= max_hits:
                        return "\n".join(hits) + f"\n(stopped at {max_hits} hits)"
        except OSError:
            continue
    return "\n".join(hits) if hits else f"no matches for {pattern!r} in {glob}"


def get_logs(trace_id: str = "", service: str = "", level: str = "", limit: int = 40) -> str:
    """Query the log store for lines around an error. Filters combine (AND).

    Args:
        trace_id: Only lines from this trace.
        service: Only lines from this service.
        level: Only lines at this level (INFO, ERROR, ...).
        limit: Most recent N matching lines.
    """
    # Scoped to the current run, so an earlier run's lines can never be served
    # as if they belonged to this error. That scoping is why logs.db no longer
    # has to be truncated between runs the way run.jsonl did.
    run_id = store.latest_run_id()
    if not run_id:
        return "log store is empty — nothing has been ingested yet"
    rows = store.query_logs(run_id=run_id, trace_id=trace_id, service=service,
                            level=level, limit=limit)
    if not rows:
        return "no matching log lines"
    return "\n".join(
        f"[{r['trace_id'] or '-'}] {(r['level'] or '?'):<5} "
        f"{r['service'] or '?'}: {(r['message'] or '')[:200]}" for r in rows
    )


def line_history(path: str, line: int) -> str:
    """When a specific line last changed, and in which commit. Often the whole answer.

    Args:
        path: File path relative to the repo root.
        line: The line number to trace.
    """
    p = _safe(path)
    if not p.is_file():
        return f"no such file: {path}"
    # The target repo is its own checkout, so run git where the file actually lives.
    repo = p.parent
    while repo != ROOT.parent and not (repo / ".git").exists():
        repo = repo.parent
    if not (repo / ".git").exists():
        return f"{path} is not in a git repository"
    try:
        r = subprocess.run(
            ["git", "log", "-L", f"{line},{line}:{p.relative_to(repo)}", "--max-count=3",
             "--date=short", "--format=%h %ad %an: %s"],
            cwd=repo, capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"git timed out tracing {path}:{line}"
    if r.returncode != 0:
        return f"git could not trace that line: {r.stderr.strip()[:200]}"
    return r.stdout[:4000] or f"no recorded history for {path}:{line}"


TOOLS = (read_source, search_code, get_logs, line_history)


def _self_check():
    global ROOT, GIT_TIMEOUT
    # The trust boundary is the part that must not regress.
    for bad in ("../../../etc/passwd", "/etc/passwd", "targets/../../secrets"):
        try:
            _safe(bad)
            raise AssertionError(f"path jail let {bad!r} through")
        except ValueError:
            pass
    assert _safe("triagelab/core/triage.py").name == "triage.py"

    # The jail must still reach targets/. If ROOT ever narrows to this package,
    # every assertion above still passes while read_source goes blind to the only
    # code it exists to read — so assert the reach, not just the refusals.
    assert ROOT.name != "toolserver", f"jail root collapsed onto the package: {ROOT}"
    assert (ROOT / "triagelab").is_dir(), f"jail root is not the repo root: {ROOT}"
    clone = ROOT / "targets" / "pyyaml" / "lib" / "yaml" / "constructor.py"
    if clone.exists():
        assert _safe(str(clone)).is_file(), "targets/ fell outside the jail"
        assert "construct_yaml_bool" in read_source(
            "targets/pyyaml/lib/yaml/constructor.py", 230, 245)

    # The narrowed jail: a real-harness run must not be able to read the answer
    # sheet. This is the exact leak observed live — search_code('bool_values')
    # returned realapp.py's FAULTS table, injection diff and truth text included.
    os.environ["TRIAGE_TOOL_JAIL"] = "targets"
    try:
        try:
            _safe("triagelab/harness/realapp.py")
            raise AssertionError("the jail let the harness's FAULTS table through")
        except ValueError:
            pass
        if clone.exists():
            assert _safe("targets/pyyaml/lib/yaml/constructor.py").is_file()
            leak = search_code("bool_values", "**/*.py")
            assert "realapp.py" not in leak, leak[:300]
            assert "constructor.py" in leak, leak[:300]
    finally:
        del os.environ["TRIAGE_TOOL_JAIL"]

    out = read_source("triagelab/core/triage.py", 1, 5)
    assert "triagelab/core/triage.py lines 1-5" in out and "\n    1 " in out, out
    assert "no such file" in read_source("nope.py")

    hits = search_code(r"def fingerprint", "triagelab/**/*.py")
    assert "triagelab/core/triage.py:" in hits, hits
    assert "no matches" in search_code(r"zzz_not_here_zzz", "triagelab/**/*.py")
    assert "bad regex" in search_code("(unclosed", "triagelab/**/*.py")

    # search_code walks glob() results, which _safe() never sees on their own:
    # a literal ".." segment is a traversal step, not a wildcard. This caught a
    # real escape — the sibling's contents came back verbatim before the fix.

    real_root = ROOT
    try:
        tmp = Path(tempfile.mkdtemp()).resolve()  # macOS /var -> /private/var
        (tmp / "root").mkdir()
        (tmp / "root" / "inside.py").write_text("marker_inside\n", encoding="utf-8")
        (tmp / "sibling.py").write_text("marker_outside\n", encoding="utf-8")
        ROOT = tmp / "root"
        escaped = search_code("marker_", "../*.py")
        assert "marker_outside" not in escaped, escaped
        assert "sibling" not in escaped, escaped
        assert "marker_inside" in search_code("marker_", "*.py")
    finally:
        ROOT = real_root

    # Three legitimate answers: real history, none recorded, or git refusing a path
    # it has never seen — which is what a file moved but not yet committed looks like.
    hist = line_history("triagelab/core/triage.py", 1)
    assert any(s in hist for s in ("Initial import", "no recorded history",
                                   "could not trace", ":")), hist

    # A hung git must degrade to a string, not stall the agentic tool loop.
    real_timeout = GIT_TIMEOUT
    try:
        GIT_TIMEOUT = 0.0001
        assert "timed out" in line_history("triagelab/core/triage.py", 1)
    finally:
        GIT_TIMEOUT = real_timeout

    print("mcp_tools self-check ok — path jail holds, all four tools answer")


if __name__ == "__main__":
    _self_check()
