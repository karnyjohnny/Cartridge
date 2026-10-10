#!/usr/bin/env python
"""Performance probe: measure, don't guess (brief section 18).

Produces real numbers for the metrics the brief lists, each tagged with the
environment it was measured in. On a development sandbox these numbers are
useful only for before/after comparisons of the same code; the Dell's numbers are
the ones that count, and this script is what fills them in there.

    python scripts/perf_probe.py --db <path> --rounds 5
    python scripts/perf_probe.py --db <path> --json artifacts/perf/sandbox.json

What is measured and how:

* cold/warm startup   subprocess launches of ``run.py --selftest``; cold drops the
                      OS page cache influence only where the OS allows it, so both
                      numbers are reported honestly and the method is recorded;
* working set         GetProcessMemoryInfo on Windows, /proc/self/status elsewhere,
                      sampled while the app idles, browses and shows artwork;
* search/filter/sort  median of N real repository queries through the browser's
                      own code path (not a synthetic SQL string);
* folder scan         a real one-level discovery over the configured root;
* thumbnail latency   real decode + scale of a stored cover at display size;
* database query      median of N list_games() calls.

Every run appends to ``docs/PERFORMANCE_REPORT.md``'s JSON companion
(``artifacts/perf/``) rather than editing prose by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from cartridge import __version__                       # noqa: E402
from cartridge.app_state import AppState                # noqa: E402
from cartridge.config import environment_info           # noqa: E402
from cartridge.core import filtering                    # noqa: E402
from cartridge.core.clock import monotonic              # noqa: E402
from cartridge.scan import discovery                    # noqa: E402


def timed(fn: Callable[[], Any], rounds: int = 5) -> Dict[str, Any]:
    """Run ``fn`` ``rounds`` times and report median/min/max in milliseconds."""
    samples = []
    result = None
    for _ in range(max(1, rounds)):
        started = monotonic()
        result = fn()
        samples.append((monotonic() - started) * 1000.0)
    return {
        "median_ms": round(statistics.median(samples), 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "rounds": len(samples),
        "last_result_type": type(result).__name__,
    }


def working_set() -> Dict[str, Optional[int]]:
    from cartridge.diagnostics import process_memory

    info = process_memory()
    return {
        "working_set_bytes": info.get("working_set_bytes"),
        "peak_working_set_bytes": info.get("peak_working_set_bytes"),
        "source": info.get("source"),
    }


def measure_startup(entry: str, rounds: int = 3) -> Dict[str, Any]:
    """Cold and warm startup of the documented entry point."""
    env = dict(os.environ)
    if not sys.platform.startswith("win") and not env.get("DISPLAY"):
        env["QT_QPA_PLATFORM"] = "offscreen"
    samples = []
    for _ in range(max(1, rounds)):
        started = monotonic()
        proc = subprocess.run(
            [sys.executable, entry, "--selftest"],
            cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        samples.append((monotonic() - started) * 1000.0)
        if proc.returncode != 0:
            return {"error": "selftest exited %d" % proc.returncode}
    return {
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "rounds": len(samples),
        "note": "full process start -> window built -> first paint -> exit",
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="collection database to measure against")
    parser.add_argument("--root", help="games root for the scan measurement")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, "artifacts", "perf"))
    parser.add_argument("--json-name", default=None)
    args = parser.parse_args(argv)

    if not os.path.isdir(args.out):
        os.makedirs(args.out)

    state = AppState(db_path=args.db, offline=True)
    if not state.open_database(args.db):
        print("cannot open %s: %s" % (args.db, "; ".join(state.open_errors)))
        return 1

    repo = state.repo
    results: Dict[str, Any] = {
        "meta": {
            "tool": "scripts/perf_probe.py",
            "app_version": __version__,
            "environment": environment_info(),
            "host": platform.platform(),
            "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "rounds": args.rounds,
            "database": args.db,
            "representative_of_target": False,
            "note": (
                "Measured on the development host. These numbers are for "
                "before/after comparisons of the same code ONLY. The Windows 7 "
                "Dell is the reference hardware; run this script there for the "
                "numbers that belong in the performance budget."
            ),
        },
        "collection": repo.stats(),
    }

    print("measuring on %s (%d games)..." % (platform.system(), repo.stats()["games"]))

    results["startup_selftest"] = measure_startup(os.path.join(REPO_ROOT, "run.py"), rounds=3)
    print("  startup (process -> first paint): %s ms" % results["startup_selftest"].get("median_ms"))

    results["idle_working_set"] = working_set()
    print("  idle working set: %s" % results["idle_working_set"]["working_set_bytes"])

    all_spec = filtering.FilterSpec(sort="title")
    results["query_all"] = timed(lambda: repo.list_games(all_spec), args.rounds)
    print("  list all games: %s ms" % results["query_all"]["median_ms"])

    results["search"] = timed(
        lambda: repo.list_games(filtering.FilterSpec(query="synthetic")), args.rounds
    )
    print("  search 'synthetic': %s ms" % results["search"]["median_ms"])

    results["filter_facets"] = timed(
        lambda: repo.list_games(
            filtering.FilterSpec(genres=["RPG"], content_types=["GOG"])
        ),
        args.rounds,
    )
    print("  filter genre+type: %s ms" % results["filter_facets"]["median_ms"])

    results["sort_year_desc"] = timed(
        lambda: repo.list_games(filtering.FilterSpec(sort="year", descending=True)),
        args.rounds,
    )
    print("  sort by year desc: %s ms" % results["sort_year_desc"]["median_ms"])

    results["facets"] = timed(lambda: repo.facets(), args.rounds)
    print("  facet counts: %s ms" % results["facets"]["median_ms"])

    root = args.root or (state.root_paths()[0] if state.root_paths() else None)
    if root:
        results["folder_scan"] = timed(
            lambda: discovery.discover_root(root, detect_types=True), max(1, args.rounds // 2)
        )
        print("  root scan: %s ms" % results["folder_scan"]["median_ms"])
    else:
        results["folder_scan"] = {"note": "no root given"}

    # thumbnail latency on a real stored cover, if one exists
    cover_path = _first_cover(repo)
    if cover_path:
        from cartridge.artwork import images as image_tools

        def thumb():
            destination = os.path.join(args.out, "perf-thumb.jpg")
            return image_tools.make_thumbnail(cover_path, destination, image_tools.COVER_THUMB)

        results["thumbnail_build"] = timed(thumb, args.rounds)
        print("  thumbnail build: %s ms" % results["thumbnail_build"]["median_ms"])
        results["browsing_working_set"] = working_set()
        print("  browsing working set: %s" % results["browsing_working_set"]["working_set_bytes"])
    else:
        results["thumbnail_build"] = {"note": "no stored cover found in this collection"}

    state.shutdown()

    name = args.json_name or ("perf-%s-%s.json" % (
        platform.system().lower(), time.strftime("%Y%m%d-%H%M%S")
    ))
    path = os.path.join(args.out, name)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, default=str)
    print("")
    print("written: %s" % os.path.relpath(path, REPO_ROOT))
    print("remember: these are %s numbers. Re-run on the Dell for the budget."
          % platform.system())
    return 0


def _first_cover(repo) -> Optional[str]:
    rows = repo.db.query(
        "SELECT g.folder_path AS fp, a.rel_path AS rp FROM assets a"
        " JOIN games g ON g.id = a.game_id"
        " WHERE a.kind = 'cover' AND a.state = 'ok' LIMIT 1"
    )
    if not rows:
        return None
    candidate = os.path.join(rows[0]["fp"], rows[0]["rp"].replace("/", os.sep))
    return candidate if os.path.exists(candidate) else None


if __name__ == "__main__":
    sys.exit(main())
