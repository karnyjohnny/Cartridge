#!/usr/bin/env python
"""Credential leak scan for tracked files, working-tree files and artifacts.

Brief section 15 requires a secret scan of tracked files *and* generated
artifacts as part of the release process. This script is that scan.

Two layers, like the runtime redactor:

1. **Known values** — if the current environment actually holds credentials
   (``CARTRIDGE_IGDB_CLIENT_ID`` etc.), their exact values are searched for. This
   catches the failure mode that patterns miss: a real secret interpolated into a
   report, fixture or log.
2. **Structural patterns** — assignments and headers that reveal credentials even
   when the concrete value is unknown (``client_secret=...``, ``Bearer ...``,
   RAWG-shaped 32-hex keys, Twitch-shaped 30-char client ids).

Never scanned: ``.git`` internals (use ``git log -p`` / a history scanner for
that; see below), binary blobs, and the user's own secret store.

Exit code 1 means something matched and must be investigated before release.
A match is not automatically a leak (a test may deliberately contain a fake) —
but every match must be eyeballed and either removed or explicitly whitelisted
here with a reason.

Git history: if the repository has history, also run

    git log -p --all | python scripts/secret_scan.py --stdin

which applies the same patterns to every diff ever committed.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from typing import Dict, List, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Patterns that indicate a credential. Deliberately noisy in a *safe* direction:
# a false positive costs a minute of a human's time.
PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    # Every "assignment" pattern is same-line only (`[ \t]*`, never `\s*`): a
    # newline between the name and the next word is code, not a credential.
    ("assignment: client_secret", re.compile(r"(?i)\bclient_secret[ \t]*[=:][ \t]*['\"]?[A-Za-z0-9_\-\.]{12,}")),
    ("assignment: client_id", re.compile(r"(?i)\bclient_id[ \t]*[=:][ \t]*['\"]?[a-z0-9]{20,}")),
    ("assignment: api key", re.compile(r"(?i)\b(api_?key|apikey)[ \t]*[=:][ \t]*['\"]?[A-Za-z0-9_\-]{16,}")),
    ("assignment: password", re.compile(r"(?i)\b(password|passwd|pwd)[ \t]*[=:][ \t]*\S{6,}")),
    ("assignment: token", re.compile(r"(?i)\b(access_?token|refresh_?token|token)[ \t]*[=:][ \t]*['\"]?[A-Za-z0-9_\-\.]{16,}")),
    ("header: authorization", re.compile(r"(?i)\bauthorization[ \t]*:[ \t]*(?!REDACTED|redacted|\*)\S+")),
    ("header: client-id", re.compile(r"(?i)\bclient-id[ \t]*:[ \t]*[a-z0-9]{20,}")),
    ("bearer token", re.compile(r"(?i)\bbearer[ \t]+[A-Za-z0-9_\-\.]{16,}")),
    # Shape-based keys, but only in an assignment/parameter context: RAWG image
    # URLs legitimately contain bare 32-hex hashes, and flagging those made the
    # scanner useless on fixtures.
    ("rawg-shaped key param", re.compile(r"(?i)[?&]key=[0-9a-f]{32}\b")),
    ("rawg-shaped key assigned", re.compile(r"(?i)\b(?:api_?key|rawg_?key)[ \t]*[=:][ \t]*['\"]?[0-9a-f]{32}\b")),
    ("twitch-shaped id assigned", re.compile(r"(?i)\bclient_?id[ \t]*[=:][ \t]*['\"]?[a-z0-9]{30}\b")),
    ("aws-shaped key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# Value shapes that are code, not credentials. An "assignment" whose value is an
# ALL_CAPS constant or a function call is a false positive by construction.
CODE_VALUE = re.compile(r"^['\"]?(?:[A-Z][A-Z0-9_]*|[A-Za-z_][A-Za-z0-9_]*\(|self\.|os\.|re\.)")

# Files whose *purpose* is to contain these strings as documentation or as
# negative tests. Each entry needs a reason.
WHITELIST = {
    os.path.join("scripts", "secret_scan.py"): "the scanner's own pattern table",
    os.path.join("cartridge", "core", "redaction.py"): "documents the patterns it scrubs",
    os.path.join("docs", "DECISIONS.md"): "discusses credential handling in prose",
    os.path.join("docs", "WINDOWS7_LIVE_TEST.md"): "shows example env var names, no values",
    os.path.join("tests", "test_redaction.py"): "fake values used to prove redaction",
    os.path.join("tests", "test_providers.py"): "fake values used to prove scrubbing",
    os.path.join("tests", "test_config.py"): "fake values for precedence tests",
    os.path.join("tests", "test_live_fixtures.py"): "a fake RAWG-shaped key used to prove the sanitizer strips it",
    os.path.join("README.md"): "documents env var names without values",
    os.path.join("packaging", "BUILD_WINDOWS7.md"): "documents the scan itself",
}

SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "build", "dist-ignored",
}

SCAN_DIRS = ("cartridge", "scripts", "tests", "docs", "packaging", "artifacts", "dist")
SCAN_FILES = ("run.py", "README.md", "CHANGELOG.md", "QWEN.md", "requirements.txt",
              "requirements-dev.txt", "pytest.ini", ".gitignore", "LICENSE")

ENV_SECRET_VARS = (
    "CARTRIDGE_IGDB_CLIENT_ID",
    "CARTRIDGE_IGDB_CLIENT_SECRET",
    "CARTRIDGE_RAWG_API_KEY",
)


def iter_files() -> List[str]:
    out: List[str] = []
    for name in SCAN_FILES:
        path = os.path.join(REPO_ROOT, name)
        if os.path.isfile(path):
            out.append(path)
    for directory in SCAN_DIRS:
        base = os.path.join(REPO_ROOT, directory)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for name in sorted(filenames):
                if name.endswith((".png", ".jpg", ".jpeg", ".gif", ".db", ".pyc")):
                    continue
                out.append(os.path.join(dirpath, name))
    return out


def scan_text(text: str, known: Dict[str, str]) -> List[str]:
    findings: List[str] = []
    for value_name, value in known.items():
        if value and value in text:
            findings.append("exact value of %s" % value_name)
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            snippet = match.group(0)
            # A pattern hit whose secret part is a placeholder is not a leak.
            lowered = snippet.lower()
            if "redacted" in lowered or "fake" in lowered or "example" in lowered \
                    or "xxxx" in lowered or "your-" in lowered or "<" in snippet:
                continue
            # Nor is one whose "value" is obviously code (a constant, a call).
            if label.startswith("assignment:") or label.endswith("assigned"):
                value = snippet.split("=", 1)[-1].split(":", 1)[-1].strip()
                if CODE_VALUE.match(value):
                    continue
                if not any(ch.isdigit() for ch in value) and len(value) < 24:
                    continue
            findings.append("%s -> %s" % (label, snippet[:60]))
    return findings


def scan_file(path: str, known: Dict[str, str]) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ["unreadable file"]
    return scan_text(text, known)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--stdin", action="store_true",
        help="scan text from stdin (use with `git log -p --all |`) instead of files",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    known = {
        name: os.environ.get(name, "")
        for name in ENV_SECRET_VARS
    }
    known = {k: v for k, v in known.items() if v}

    if args.stdin:
        text = sys.stdin.read()
        findings = scan_text(text, known)
        if findings:
            print("SECRET SCAN: %d finding(s) in stdin" % len(findings))
            for item in findings[:40]:
                print("  " + item)
            return 1
        print("SECRET SCAN: stdin clean (%d known-value checks, %d patterns)"
              % (len(known), len(PATTERNS)))
        return 0

    total = 0
    problems = 0
    for path in iter_files():
        rel = os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")
        total += 1
        findings = scan_file(path, known)
        if not findings:
            continue
        if rel in WHITELIST:
            if not args.quiet:
                print("  whitelisted %-46s %s" % (rel, WHITELIST[rel]))
            continue
        problems += 1
        print("SECRET SCAN FINDING in %s" % rel)
        for item in findings[:10]:
            print("    " + item)

    print("-" * 72)
    print("scanned %d files · %d known secret values checked · %d patterns"
          % (total, len(known), len(PATTERNS)))
    if not known:
        print("note: no credentials were present in this environment, so the")
        print("      known-value layer could not run. Pattern layer still did.")
    if problems:
        print("RESULT: FAIL - investigate the findings above before any release.")
        print("        If a real credential leaked: stop distributing the artifact,")
        print("        remove it, and rotate the credential. Do not paste it here.")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
