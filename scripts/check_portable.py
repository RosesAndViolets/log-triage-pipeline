"""Catch the things that work on macOS and break on Windows.

Every failure here is one that a mac cannot reproduce by running the code:
the platform differences are silent locally and fatal on the other machine.
Run it before pushing anything you intend to clone onto Windows.

    python check_portable.py
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:      # runnable directly, not only via run.py
    sys.path.insert(0, str(ROOT))
OURS = sorted(p for p in (ROOT / "triagelab").rglob("*.py")) + [ROOT / "run.py"]

# Text I/O that omits `encoding=`. Windows defaults to the locale code page
# (cp1252 on most machines), so any non-ASCII in this repo decodes wrong or
# raises. Binary modes are exempt — they have no encoding.
IO_CALLS = {"open", "read_text", "write_text"}


def _text_io_without_encoding(tree: ast.AST) -> list[tuple[int, str]]:
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
        if name not in IO_CALLS:
            continue
        kw = {k.arg for k in node.keywords}
        if "encoding" in kw:
            continue
        mode = ""
        if name == "open" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = str(node.args[1].value)
        for k in node.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = str(k.value.value)
        if "b" in mode:
            continue                      # binary: no encoding to specify
        bad.append((node.lineno, name))
    return bad


def _pathish(node: ast.AST) -> bool:
    """Does this expression look like it holds a filesystem path?"""
    name = ""
    if isinstance(node, ast.Name):
        name = node.id
    elif isinstance(node, ast.Attribute):
        name = node.attr
    return bool(re.search(r"path|file|dir|src|stack|target", name, re.I))


def _hardcoded_separators(tree: ast.AST) -> list[tuple[int, str]]:
    """Path logic that assumes '/'. Windows tracebacks carry backslashes.

    Walked as AST rather than matched as text, so a comment explaining this
    very rule does not trip it — which the regex version did.
    """
    bad = []
    for node in ast.walk(tree):
        # "targets/" in some_path
        if isinstance(node, ast.Compare) and node.ops and \
                isinstance(node.ops[0], (ast.In, ast.NotIn)):
            left, right = node.left, node.comparators[0]
            if isinstance(left, ast.Constant) and isinstance(left.value, str) \
                    and "/" in left.value and _pathish(right):
                bad.append((node.lineno, f'"{left.value}" in {ast.unparse(right)[:40]}'))
        # some_path.split("targets/")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == "split" and node.args:
            a = node.args[0]
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and "/" in a.value and _pathish(node.func.value):
                bad.append((node.lineno, f'.split("{a.value}")'))
    return bad


def main() -> int:
    problems: list[str] = []

    for path in OURS:
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(path))

        for lineno, name in _text_io_without_encoding(tree):
            problems.append(f"{path.name}:{lineno}: {name}() without encoding= "
                            f"— Windows will use cp1252")

        for lineno, snippet in _hardcoded_separators(tree):
            problems.append(f"{path.name}:{lineno}: path compared as a string with '/' "
                            f"— use Path/relative_to: {snippet}")

        # os.path.join with a literal separator, and shell-only helpers
        for lineno, line in enumerate(src.splitlines(), 1):
            if re.search(r"subprocess\.(run|Popen)\([^)]*shell=True", line):
                problems.append(f"{path.name}:{lineno}: shell=True — cmd.exe is not sh")
            if "/tmp/" in line and not line.strip().startswith("#"):
                problems.append(f"{path.name}:{lineno}: hardcoded /tmp — use tempfile")
            if re.search(r"\bos\.uname\b|\bsignal\.SIGKILL\b|\bos\.fork\b", line):
                problems.append(f"{path.name}:{lineno}: POSIX-only API")

    # The DB files must be ignored, or a clone drags another machine's runs along.
    gitignore = (ROOT / ".gitignore")
    ig = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    for want in ("data/", "map.html"):
        if want not in ig:
            problems.append(f".gitignore: missing {want}")

    if problems:
        print(f"portability: {len(problems)} problem(s)\n")
        for p in problems:
            print("  " + p)
        return 1

    print(f"portability ok — {len(OURS)} modules: text I/O is explicit utf-8, "
          f"paths are separator-agnostic, no POSIX-only calls")
    return 0


if __name__ == "__main__":
    from triagelab.core import store

    store.use_utf8_stdout()
    sys.exit(main())
