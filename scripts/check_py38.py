"""Python 3.8 compatibility gate (brief section 3 and DECISIONS D-011).

The development sandbox runs Python 3.11 while the product target is 3.8. Running
the test suite on 3.11 proves nothing about 3.8, so compatibility is enforced
statically, in two independent ways:

1. **vermin** parses every module and computes the minimum interpreter version
   required by the syntax and stdlib APIs actually used.
2. **A forbidden-construct scan** for patterns that are easy to slip in and that
   a version detector may not flag in every position (PEP 604 ``X | Y`` unions in
   annotations, builtin generics used at runtime, ``match`` statements, 3.9+
   string methods, ``functools.cache``...).

Exit code 0 means the tree is safe for the Dell. Anything else must be fixed
before packaging — a ``SyntaxError`` on launch is the single worst failure mode
for an app that has to run on a machine we cannot debug remotely.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = "3.8"
SCAN_ROOTS = ("cartridge", "scripts", "tests")
EXTRA_FILES = ("run.py",)

# (regex, explanation). Deliberately conservative: a false positive costs a few
# seconds of a human's time, a false negative costs a broken release.
FORBIDDEN: List[Tuple[str, str]] = [
    (r"\bmatch\s+[^\n=]+\s*:\s*\n\s*case\b", "structural pattern matching (3.10+)"),
    (r"\bremoveprefix\s*\(", "str.removeprefix (3.9+) - use slicing"),
    (r"\bremovesuffix\s*\(", "str.removesuffix (3.9+) - use slicing"),
    (r"\bfunctools\.cache\b", "functools.cache (3.9+) - use lru_cache(maxsize=None)"),
    (r"\basyncio\.to_thread\b", "asyncio.to_thread (3.9+)"),
    (r"\bhashlib\.file_digest\b", "hashlib.file_digest (3.11+) - hash in chunks"),
    (r"\bzoneinfo\b", "zoneinfo (3.9+)"),
    (r"\bgraphlib\b", "graphlib (3.9+)"),
    (r"\bmath\.lcm\b", "math.lcm (3.9+)"),
    (r"\bmath\.nextafter\b", "math.nextafter (3.9+)"),
    (r"\bshutil\.copytree\([^)]*dirs_exist_ok", "dirs_exist_ok (3.8+ ok, but verify)"),
    (r"\bis_junction\s*\(", "Path.is_junction (3.12+)"),
    (r"\bdataclasses\.KW_ONLY\b|\bKW_ONLY\b", "dataclasses.KW_ONLY (3.10+)"),
    (r"\bimportlib\.resources\.files\b", "importlib.resources.files (3.9+)"),
    (r"\bstr\s*\|\s*None\b|\bint\s*\|\s*None\b|\bfloat\s*\|\s*None\b|"
     r"\bbool\s*\|\s*None\b|\bbytes\s*\|\s*None\b|\bdict\s*\|\s*None\b|"
     r"\blist\s*\|\s*None\b",
     "PEP 604 union syntax (3.10+) - use typing.Optional"),
    (r"\bdict\s*\|\s*dict\b", "PEP 584 dict union operator (3.9+) - use .update()"),
    (r":\s*(list|dict|set|tuple|frozenset|type)\s*\[",
     "builtin generic annotation (3.9+) - use typing.List/Dict/... "
     "or add `from __future__ import annotations`"),
    (r"->\s*(list|dict|set|tuple|frozenset)\s*\[",
     "builtin generic return annotation (3.9+) - use typing.List/Dict/..."),
    (r"\btyping\.TypeAlias\b", "typing.TypeAlias (3.10+)"),
    (r"\btyping\.ParamSpec\b", "typing.ParamSpec (3.10+)"),
    (r"\btyping\.Self\b", "typing.Self (3.11+)"),
    (r"\btyping\.LiteralString\b", "typing.LiteralString (3.11+)"),
    (r"\bExceptionGroup\b|\bexcept\s*\*", "exception groups / except* (3.11+)"),
    (r"\bcontextlib\.aclosing\b", "contextlib.aclosing (3.10+)"),
    (r"\bitertools\.pairwise\b", "itertools.pairwise (3.10+)"),
    (r"\bsys\.stdout\.reconfigure\b", "reconfigure is 3.7+ but encoding on Windows "
                                      "consoles needs care - verify manually"),
]

# Lines matching these are allowed to mention a forbidden construct (e.g. this
# file's own table, or a comment explaining why something is avoided).
ALLOW_CONTEXTS = (
    os.path.join("scripts", "check_py38.py"),
)


def iter_python_files() -> List[str]:
    out = []
    for root in SCAN_ROOTS:
        base = os.path.join(REPO_ROOT, root)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [
                d for d in dirnames
                if d not in ("__pycache__", ".pytest_cache", ".venv", "build", "dist")
            ]
            for name in sorted(filenames):
                if name.endswith(".py"):
                    out.append(os.path.join(dirpath, name))
    for extra in EXTRA_FILES:
        candidate = os.path.join(REPO_ROOT, extra)
        if os.path.exists(candidate):
            out.append(candidate)
    return out


def docstring_lines(path: str) -> set:
    """Line numbers that belong to a docstring.

    A docstring that *mentions* a forbidden API (usually to say why it is
    avoided) is documentation, not a violation. Everything else - code and
    comments - is still scanned.
    """
    import ast

    lines = set()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
    except (OSError, SyntaxError, UnicodeDecodeError):
        return lines
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                start = first.lineno
                end = getattr(first, "end_lineno", start) or start
                lines.update(range(start, end + 1))
    return lines


def scan_forbidden(files: List[str]) -> List[str]:
    problems = []
    for path in files:
        rel = os.path.relpath(path, REPO_ROOT)
        if any(rel.replace("/", os.sep).endswith(ctx) for ctx in ALLOW_CONTEXTS):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except (OSError, UnicodeDecodeError) as exc:
            problems.append("%s: cannot read (%s)" % (rel, exc))
            continue
        skipped = docstring_lines(path)
        for lineno, line in enumerate(lines, start=1):
            if lineno in skipped:
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for pattern, why in FORBIDDEN:
                if re.search(pattern, line):
                    problems.append("%s:%d: %s  ->  %s" % (rel, lineno, why, stripped[:90]))
    return problems


def run_vermin(files: List[str]) -> Tuple[bool, str]:
    """Run vermin over the tree; return (ok, human-readable output)."""
    targets = [os.path.join(REPO_ROOT, root) for root in SCAN_ROOTS]
    targets = [t for t in targets if os.path.exists(t)]
    targets += [os.path.join(REPO_ROOT, f) for f in EXTRA_FILES
                if os.path.exists(os.path.join(REPO_ROOT, f))]

    candidates = [
        ["vermin"],
        [sys.executable, "-m", "vermin"],
    ]
    last_error = "vermin not found"
    for prefix in candidates:
        cmd = prefix + ["--target=%s-" % TARGET, "--no-tips", "--violations"] + targets
        try:
            proc = subprocess.run(
                cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
            )
        except (OSError, FileNotFoundError) as exc:
            last_error = str(exc)
            continue
        output = proc.stdout.decode("utf-8", "replace").strip()
        if "No module named" in output or "not found" in output.lower() and proc.returncode != 0:
            last_error = output.splitlines()[0] if output else "vermin unavailable"
            continue
        ok = proc.returncode == 0
        return ok, output
    return False, "vermin could not be executed: %s" % last_error


def main() -> int:
    files = iter_python_files()
    print("Python %s compatibility gate (target: %s)" % (sys.version.split()[0], TARGET))
    print("scanning %d files" % len(files))
    print("-" * 72)

    failed = False

    problems = scan_forbidden(files)
    if problems:
        failed = True
        print("FORBIDDEN CONSTRUCTS (%d):" % len(problems))
        for item in problems:
            print("  " + item)
    else:
        print("forbidden-construct scan : OK")

    ok, output = run_vermin(files)
    print("vermin                   : %s" % ("OK" if ok else "FAIL"))
    for line in output.splitlines():
        if line.strip():
            print("  " + line.rstrip())
    if not ok:
        failed = True

    # Syntax must at least parse with the 3.8 grammar.
    import ast

    parse_failures = []
    for path in files:
        rel = os.path.relpath(path, REPO_ROOT)
        try:
            with open(path, "rb") as handle:
                source = handle.read()
            ast.parse(source, filename=rel, feature_version=(3, 8))
        except SyntaxError as exc:
            parse_failures.append("%s:%s: %s" % (rel, exc.lineno, exc.msg))
    if parse_failures:
        failed = True
        print("ast.parse(feature_version=(3,8)) : FAIL")
        for item in parse_failures:
            print("  " + item)
    else:
        print("ast 3.8 grammar parse    : OK (%d files)" % len(files))

    print("-" * 72)
    print("RESULT: %s" % ("FAIL - fix before packaging" if failed else "PASS"))
    print("NOTE: a static gate is not a run on Python 3.8. The runtime claim stays")
    print("      NOT TESTED until executed on the Dell (docs/WINDOWS7_LIVE_TEST.md §2).")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
