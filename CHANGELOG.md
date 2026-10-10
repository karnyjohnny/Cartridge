# CHANGELOG

All notable changes to Cartridge. Dates are UTC. This project keeps honest
records: anything not verified on the Windows 7 target is labelled as such in
`docs/STATUS.md` rather than implied here.

## 0.1.2 — 2026-10-10

Second field-report release from the Windows 7 run: one crash, several UX gaps
and a theme bug that only top-level dialogs exposed.

### Fixed

- **Crash: `NameError: QCheckBox` in the content-type picker** (Manager →
  right-click → "Edit content types…"). A missing import; the widget was only
  reachable through that menu, which no test exercised. Regression test added.
- **Dialogs painted transparent.** In the stylesheet the generic `QWidget`
  background rule came before the `QMainWindow, QDialog` rule; Qt resolves
  equal-specificity type selectors by order, so every dialog lost its background
  (white on a light compositor, pure black under the Win7 classic theme). Rules
  reordered; regression test grabs a dialog and asserts >90% dark pixels.
- **"Edit content types…" on an unmatched folder appeared dead** (it only wrote a
  status-bar line). It now asks whether to add the folder to the catalogue first
  and opens the import flow with that folder pre-filled.

### Improved (field requests)

- Manager context menu reordered and gated: **Open folder** first (the everyday
  action), add/edit/types/measure in the middle, destructive entries last and
  only when they apply; "Re-associate…" only shows when the folder is missing.
  "Measure folder size" now also works on unmatched folders (reports the number
  without pretending to store it).
- Add-game dialog gained an **Open folder** button on the folder step, so a folder's
  contents can be verified in Explorer without leaving the import flow.
- Add-game result preview rebuilt: cover sits **beside** the title (no more
  floating "No cover" box over the text) and the selected result's cover is
  actually fetched and shown.
- Result covers now **stream in the background for every row**, in rank order and
  paced by the image rate limiter, instead of only for the selected row. Downloads
  are cancellable and their temp directory is removed when the dialog closes.

### Tests

571 offline tests (6 new regressions, including a full offline search flow through
the import dialog with a mocked provider transport that streams real PNG bytes).


## 0.1.1 — 2026-10-10

Field-report release. The first real Windows 7 run (packaged build, Python
3.8.20) found two bugs in the fresh-install path; both are fixed here with
regression tests that reproduce the exact scenario.

### Fixed

- **First-run "Create collection" failed with "No database path is configured."**
  `_on_continue` never derived the database path from the chosen root on a fresh
  install (it only handled the unwritable-root fallback). It now resolves
  `<root>\.cartridge\collection.db` exactly as the dialog promises.
- **Settings → "Add root folder…" did nothing at all.** With no database open
  (the user had skipped first-run), the slot hit `AttributeError` on a `None`
  repository; on a windowed Windows build an exception in a Qt slot only reaches
  an invisible stderr, so the click appeared dead. Settings now creates/opens the
  database for the chosen root, and the slot wraps its work so any failure shows
  a dialog instead of vanishing.
- **Working-set measurement failed on Windows 7** (`GetProcessMemoryInfo failed`).
  The probe now declares ctypes argtypes/restypes for the process handle, tries
  `psapi.GetProcessMemoryInfo`, `kernel32.K32GetProcessMemoryInfo` and
  `kernel32.GetProcessMemoryInfo` in turn, and records the Win32 error code of
  every attempt in Diagnostics instead of a bare "failed".
- A directory-listing order assumption made one artwork test flaky across
  filesystems (sorted comparison now).

### Verified by the field run (recorded in docs/STATUS.md and TEST_REPORT §8)

- the **frozen PyInstaller build launches on Windows 7 SP1 x64** (packaged build
  True in the user's diagnostics report);
- the application **runs on Python 3.8.20** (the static 3.8 gate is confirmed by
  an actual 3.8 interpreter);
- the **DPAPI credential store works on Windows**: backend `dpapi`, encrypted,
  keys present after saving from Settings;
- providers configured and rate-limit counters advancing from real use.

### Not yet re-verified on the target

- the fixed fresh-install flow (0.1.1) and the memory probe; a new diagnostics
  report from the Dell closes both.

## 0.1.0 — 2026-10-09

First complete implementation of the project brief (milestones M0–M10, M13 in the
development sandbox; M11/M12 pending the target machine).

### Added

- **Collection Browser**: recycling cover grid + compact list, debounced search,
  facet filters, sorts with NULLs last, details pane with screenshots, favourites.
- **Manager**: five distinct problem states, background rescan, re-association
  with explicit confirmation, explicit record deletion only.
- **Import dialog**: the reference 11-step flow; IGDB search with confidence
  bands; RAWG fallback; artwork selection; manual entry; weak-match confirmation.
- **Providers**: IGDB v4 through Twitch OAuth2 (token cached in memory, re-auth on
  401, Apicalypse escaping, three-step query degradation), RAWG REST, defensive
  payload mapping, local re-ranking, cross-provider dedupe.
- **Rate limiting**: token bucket + per-minute/day budgets below published limits
  (IGDB ≤3 rps & 1500/day vs published 4 rps & 40k/day; RAWG 250/day vs the
  20k/month free tier), `Retry-After` and `X-RateLimit-*` support, refusals
  instead of long sleeps.
- **Artwork**: streamed downloads with byte caps, magic-byte + truncation + decode
  validation, atomic finalize, SHA-256 and dimensions recorded, on-disk thumbnails,
  bounded LRU decode cache, manifests without credentials.
- **Persistence**: versioned SQLite schema with migrations, folder-path identity
  (Windows case/separator aware), provider-vs-user column separation, transactional
  imports, online backup API.
- **Secrets**: DPAPI (ctypes) / 0600-file / session-only store with the active
  backend reported honestly; redactor on every error, log and export path;
  release-time `scripts/secret_scan.py`.
- **Diagnostics**: shareable sanitized report vs explicit detailed local export.
- **Tooling**: `run.py --selftest`, `scripts/smoke_test.py` (M1 proof),
  `scripts/check_py38.py`, `scripts/gen_demo_data.py` (labelled synthetic data),
  `scripts/perf_probe.py`, `scripts/screenshot.py` (real renders),
  `packaging/cartridge.spec` + `packaging/BUILD_WINDOWS7.md`.
- **Docs**: QWEN.md, README, STATUS, DECISIONS (14 entries), TEST_REPORT,
  PERFORMANCE_REPORT, WINDOWS7_LIVE_TEST.

### Verified

- 561 offline automated tests; 37 of them drive the real widgets offscreen.
- Live IGDB + RAWG verification: 12/12 checks, including real cover/image
  downloads and the broken-credentials → RAWG fallback path.
- Performance on 1,000 synthetic games (development sandbox): query-all 34.8 ms,
  search 36.1 ms, five viewport scrolls 45.1 ms, 22 live card widgets, browsing
  working set 61.4 MB. Dell numbers: NOT MEASURED.
- Python 3.8 static gate PASS over 79 files; secret scan PASS including with
  credentials loaded.

### Fixed during development (all regression-tested)

- JPEG truncation check read the first bytes instead of the last (found by the
  live run).
- Twitch HTTP 400 on a bad secret reported as a generic request rejection.
- `is_writable_location()` created the directory it probed.
- `setText()` on a read-only `QLineEdit` aborts the process on Qt ≥ 5.15.
- Four Qt worker-pool lifetime faults that segfaulted instead of raising
  (recorded as DECISIONS D-012).
- Recycled cards left ghost badges (`deleteLater` kept them parented).
- RAWG list endpoint has no developers/publishers → `enrich()` detail call.

### Not verified (deliberately disclosed)

- Execution on Windows 7 SP1 and on Python 3.8 (checklist: WINDOWS7_LIVE_TEST.md).
- Frozen PyInstaller build launch on the target.
- DPAPI credential store round-trip (Windows-only code path).
