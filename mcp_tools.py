"""The triage toolset: what the model can pull for itself.

Plain functions, deliberately. MCP is a thin wrapper over this module
(`mcp_server.py`), so these can be tested — and called — with no server, no
protocol and no API key involved.

Everything here is stateless: each call takes what it needs as arguments and
reads from disk. Nothing is remembered between calls, so restarting the server
loses nothing.
"""

import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.resolve()
LOG_STORE = ROOT / "run.jsonl"
MAX_SPAN = 200  # lines per read_source call — the model can ask again, tokens are the budget


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
    lines = p.read_text(errors="replace").splitlines()
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
    for p in sorted(ROOT.glob(glob)):
        if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
            continue
        try:
            for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
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
    if not LOG_STORE.exists():
        return "log store is empty — nothing has been ingested yet"
    out = []
    for raw in LOG_STORE.read_text().splitlines():
        try:
            e = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if trace_id and e.get("trace_id") != trace_id:
            continue
        if service and e.get("serviceName") != service:
            continue
        if level and e.get("level") != level.upper():
            continue
        out.append(f"[{e.get('trace_id','-')}] {e.get('level','?'):<5} "
                   f"{e.get('serviceName','?')}: {e.get('message','')[:200]}")
    if not out:
        return "no matching log lines"
    return "\n".join(out[-limit:])


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
    r = subprocess.run(
        ["git", "log", "-L", f"{line},{line}:{p.relative_to(repo)}", "--max-count=3",
         "--date=short", "--format=%h %ad %an: %s"],
        cwd=repo, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"git could not trace that line: {r.stderr.strip()[:200]}"
    return r.stdout[:4000] or f"no recorded history for {path}:{line}"


TOOLS = (read_source, search_code, get_logs, line_history)


def _self_check():
    # The trust boundary is the part that must not regress.
    for bad in ("../../../etc/passwd", "/etc/passwd", "targets/../../secrets"):
        try:
            _safe(bad)
            raise AssertionError(f"path jail let {bad!r} through")
        except ValueError:
            pass
    assert _safe("triage.py").name == "triage.py"

    out = read_source("triage.py", 1, 5)
    assert "triage.py lines 1-5" in out and "\n    1 " in out, out
    assert "no such file" in read_source("nope.py")

    hits = search_code(r"def fingerprint", "*.py")
    assert "triage.py:" in hits, hits
    assert "no matches" in search_code(r"zzz_not_here_zzz", "*.py")
    assert "bad regex" in search_code("(unclosed", "*.py")

    hist = line_history("triage.py", 1)
    assert "Initial import" in hist or "no recorded history" in hist, hist

    print("mcp_tools self-check ok — path jail holds, all four tools answer")


if __name__ == "__main__":
    _self_check()
