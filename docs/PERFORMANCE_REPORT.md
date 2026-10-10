# PERFORMANCE_REPORT.md

**Rule for this file: measured numbers only.** No estimates, no targets presented
as results, no numbers copied from a modern machine and attributed to the Dell.
Every row states where it was measured, on what data, with which versions, and how.

---

## Measurement environments

| ID | Machine | OS | Python | Qt | Validity |
|----|---------|----|--------|----|----------|
| `SANDBOX` | agent container, 2 vCPU, container disk | Debian 12, headless (`QT_QPA_PLATFORM=offscreen`) | 3.11.2 | 5.15.2 | **Not the target.** Useful only for before/after comparison of the same code. No GMA 4500MHD, no mechanical disk, no 2 GB ceiling. |
| `DELL` | Dell Latitude E5500 (Core 2 Duo T7250, 2 GB DDR2, GMA 4500MHD, 1280×800, HDD) | Windows 7 SP1 x64 | 3.8.20 (intended) | 5.15.2 (intended) | **The only environment whose numbers set the budget.** Not yet measured by the agent. |

Tool: `scripts/perf_probe.py` (plus the GUI render measurements below, which the
probe cannot take because they need a live window). Data set:
`scripts/gen_demo_data.py --games 1000 --with-artwork --screenshots 2 --with-sizes`
— 1,000 synthetic, clearly labelled records, 882 with a locally drawn cover.

---

## Results — SANDBOX (2026-10-09, app v0.1.0, 1,000 synthetic games)

| Metric | Value | Conditions |
|---|---|---|
| Process start → window built → first paint (`run.py --selftest`) | **315.8 ms** median (min 311, max 322, n=3) | full subprocess, offscreen plugin |
| Window construct in-process (state + DB open + MainWindow) | **399.7 ms** median (n=5) | 1,000-game DB, first call includes module import |
| First paint after `show()` + initial query | **222.0 ms** median (n=5) | grid populated with 1,000 rows |
| Idle working set | **36.2 MB** (37,974,016 B) | after DB open, before browsing |
| Browsing working set | **61.4 MB** (64,401,408 B) | after rendering 1,000 rows + thumbnails |
| Repository query, all 1,000 rows (hydrated) | **34.8 ms** median (n=7) | `list_games(FilterSpec())` |
| Query + card-pool populate + paint | **52.6 ms** median (n=7) | the browser's real code path |
| Search "synthetic" (LIKE over folded blob, 1,000 rows) | **36.1 ms** median (n=7) | repository level |
| Filter genre + content type | **1.4 ms** median (n=7) | EXISTS subqueries |
| Sort by year, descending (NULLs last) | **34.1 ms** median (n=7) | repository level |
| Facet counts (genres/types/decades/devs/platforms) | **2.2 ms** median (n=7) | five GROUP BY queries |
| Facet rebuild into sidebar chips | **3.8 ms** median (n=7) | includes widget rebuild |
| Root scan, 1,000 folders + bounded type detection | **61.4 ms** median (n=3) | one level, 200 names/folder cap |
| Five viewport scrolls (card recycling) | **45.1 ms** total median (n=5) | ~9 ms per viewport re-render |
| Thumbnail build from a stored cover | **0.8 ms** median (n=7) | 240×336 JPEG from 180×252 PNG |
| Card widgets alive while browsing 1,000 games | **22** | the recycling pool; independent of collection size |

Source files: `artifacts/perf/perf-sandbox-1000games.json` and the render
measurement run recorded alongside it (method described above each row).

### What these numbers prove, and what they do not

They prove the *design* holds at 1,000 records: query cost is flat in the widget
count (22 cards, not 1,000), scrolling re-renders only the viewport, and memory
stays in the tens of megabytes with artwork shown.

They prove **nothing** about the E5500: that machine has ~4× slower single-thread
performance, a mechanical disk (the 36→61 MB growth pattern will page differently),
software-limited rendering, and Python 3.8. Treat every SANDBOX number as a shape,
not a budget.

---

## Results — DELL

| Metric | Value | Conditions |
|---|---|---|
| Cold startup | **NOT MEASURED** | |
| Warm startup | **NOT MEASURED** | |
| Idle working set | **NOT MEASURED** | |
| Browsing working set | **NOT MEASURED** | |
| Peak working set (artwork view) | **NOT MEASURED** | |
| Idle CPU | **NOT MEASURED** | |
| Search / filter / sort latency | **NOT MEASURED** | |
| Folder scan time | **NOT MEASURED** | |
| Image load / thumbnail latency | **NOT MEASURED** | |
| Database query latency | **NOT MEASURED** | |
| API latency (network only) | measured live from SANDBOX, see below | not representative of the Dell's NIC |

### Live API latency (SANDBOX network, 2026-10-09, from `artifacts/live/live_api_report.md`)

| Call | Latency |
|---|---|
| Twitch OAuth2 token | 398 ms |
| IGDB search (3 titles) | 277 / 236 / — ms |
| IGDB detail by id | 252 ms |
| IGDB cover download (15,808 B) | 16 ms |
| RAWG search | 1,261 / 579 ms |
| RAWG detail | 661 ms |
| RAWG image download (362,037 B) | 46 ms |

These are network round-trips from the container. On the Dell the same calls will
be slower or faster depending on its NIC and ISP; only the *client-side* behaviour
(timeouts, retries, rate limiting) is what the app controls, and that is tested.

---

## How to measure on the Dell

1. Build or copy the source tree onto the machine (no compiler needed).
2. `python scripts\gen_demo_data.py --root C:\cartridge-perf --games 1000 --with-artwork`
   (synthetic data; or point `--db` at a real collection and skip this step).
3. `python scripts\perf_probe.py --db C:\cartridge-perf\.cartridge\demo-collection.db --root C:\cartridge-perf --rounds 7`
4. Paste the printed table into this file under a new "Results — DELL" section,
   together with `python --version`, the PyQt5/Qt versions and the record count.
5. For the GUI-only numbers (first paint, scroll), run the app, open Diagnostics,
   and read the latency samples — they are recorded from the real code path.

Working set on Windows is read through `GetProcessMemoryInfo` (`ctypes`, no extra
dependency); on Linux from `/proc/self/status`. If neither is available the field
reports `not measured` rather than a guess.

---

## Optimization log

| Date | Bottleneck (measured) | Change | Before | After | Kept? |
|---|---|---|---|---|---|
| 2026-10-09 | n/a — baseline established | recycling card grid instead of one widget per game | n/a (would be ~1,000 widgets) | 22 widgets, ~9 ms per viewport render | yes |
| 2026-10-09 | n/a — baseline | thumbnails at display size, decoded once, LRU-bounded | full-res decode per paint | 0.8 ms per thumbnail, cache hit afterwards | yes |

The loop to follow for anything new (brief §18): measure baseline → identify a
reproducible bottleneck → one focused change → targeted tests → re-measure under
comparable conditions → keep or revert → regression tests → checkpoint.
