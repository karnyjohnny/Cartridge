# STATUS.md — milestone tracker

Statuses: `NOT STARTED` · `IN PROGRESS` · `PASS` · `FAIL` · `BLOCKED` · `NOT TESTED`
Every entry carries the evidence that justifies it, or the precise evidence still missing.

**Environment split — read this before trusting any row.**

| | Development sandbox | Target machine |
|---|---|---|
| OS | Debian 12 (Linux), headless (`offscreen` plugin) | Windows 7 SP1 x64 |
| Python | 3.11.2 | 3.8.20 |
| Qt runtime | 5.15.2 | 5.15.2 |
| Access | available to the agent | **not available to the agent** |

Nothing requiring Windows 7 or Python 3.8 can be `PASS` from the sandbox. Those rows
are `NOT TESTED` with a pointer to `docs/WINDOWS7_LIVE_TEST.md`.

_Last updated: 2026-10-10, after the first Windows 7 field run and the 0.1.1 fix release._

## Milestones

| # | Milestone | Status | Evidence / what is missing |
|---|---|---|---|
| M0 | Inspect environment | **PASS** | Versions recorded in QWEN.md; `ui-reference.html` absent from the workspace → fallback design spec (brief §6) used as source of truth. |
| M1 | Compatibility proof (sandbox) | **PASS** | `scripts/smoke_test.py` → 7 PASS / 2 NOT TESTED; with `--network` → 8 PASS / 1 NOT TESTED. Reports in `artifacts/smoke/`. |
| M1-W | Compatibility proof on the Dell | **PARTIAL PASS** (field run) | User ran the packaged build on Win7 SP1 x64 / Python 3.8.20 (2026-10-10): app launches, first-run dialog and Settings render dark, diagnostics produced. `smoke_test.py` itself has not been run there yet (§1 of WINDOWS7_LIVE_TEST.md). |
| M2 | App shell + persistent settings | **PASS** | `MainWindow` with title bar/nav rail/stacked views/status bar; theme verified by render (91.9–97% dark pixels, screenshots in `artifacts/screenshots/`); settings persisted in SQLite `settings`; secrets in the secret store. 37 GUI tests. |
| M3 | SQLite model | **PASS** | Versioned schema + migrations, FK enforcement, transactional imports, user-override columns. 85 tests incl. rollback, cascade, backup, integrity, secret refusal. |
| M4 | Filesystem discovery | **PASS** | One-level scan, app-managed and system folders excluded, advisory type hints, bounded listings, cancellable; reconcile never deletes. 57 tests; verified that discovery changes nothing on disk. |
| M5 | Metadata providers | **PASS** (incl. live) | IGDB (Twitch OAuth2) + RAWG fallback over httpx, typed errors, retries, rate limiting, local ranking, manual entry. Offline 89 tests; **live 12/12 PASS** + 8 pytest live PASS; sanitized captures in `tests/fixtures/live_*.json`. |
| M6 | Artwork pipeline | **PASS** | Temp-file → validate (magic bytes, truncation, decode) → atomic finalize → hash/dimensions recorded → thumbnails; bounded LRU cache. 96 tests; live downloads of a real IGDB cover (264×352) and RAWG image (1920×1080). |
| M7 | Game Manager | **PASS** | Five distinct states, unmatched-folder discovery, add/import/edit, rescan on the worker pool, re-associate with confirmation, explicit delete only. GUI tests drive a real rescan and import. |
| M8 | Collection Browser | **PASS** | Debounced search, facets, sorts with NULLs last, recycling card grid (22 widgets for 1,000 rows), details pane, offline browsing from local DB + assets. |
| M9 | Failure / offline states | **PASS** | Offline launch (no providers configured) shows actionable empty states; provider failure surfaces redacted messages and keeps the dialog usable; missing folder/drive/artwork banners; interrupted imports roll back or mark incomplete. Covered by 38 transport tests + GUI tests. |
| M10 | Diagnostics + performance | **PASS** (sandbox) | Sanitized shareable report + explicit local export with warning; real latency samples from the code path; `scripts/perf_probe.py` numbers in `docs/PERFORMANCE_REPORT.md`. **DELL: NOT MEASURED.** |
| M11 | Packaging | **PASS (launch)** | The frozen PyInstaller build launched on Windows 7 SP1 x64 and rendered the UI (user field report 2026-10-10, `packaged build: True`). Still to verify on target: `Cartridge.exe --selftest` and §4 checklist. |
| M12 | Windows 7 live verification | **IN PROGRESS** | Field run 2026-10-10 executed parts of §3 and found two fresh-install bugs (fixed in 0.1.1, regression-tested) plus a memory-probe failure (hardened). Python 3.8 runtime and DPAPI store verified PASS by that run. Remaining: re-run with 0.1.1, then §3–§6 in full. |
| M13 | Regression + release prep | **PASS** (sandbox) | Full suite 561 passed / 8 skipped (opt-in); `check_py38.py` PASS (79 files); `secret_scan.py` PASS including with real credentials loaded; README/CHANGELOG/LICENSE/docs present; limitations disclosed. |

## Compatibility-proof detail (latest sandbox run, 2026-10-09)

```text
environment      PASS        Python 3.11.2 / PyQt5 5.15.11 / Qt 5.15.2 / httpx 0.28.1 / SQLite 3.40.1
gui_window       PASS        shown, resized (1024x700), closed, painted
gui_dark_theme   PASS        dark pixels 91.9%, corner #14161a
sqlite           PASS        write/read/reopen/integrity_check ok
httpx_local      PASS        200 + 404 + read-timeout + connect-error all typed
httpx_https      PASS (--network only)  pypi.org 200, DNS failure typed
image_pipeline   PASS        load, thumb, corrupt rejected, missing rejected
app_entry_point  PASS        `python run.py --selftest` exit 0
packaging        NOT TESTED  Windows-only; see packaging/BUILD_WINDOWS7.md
```

## Live provider verification (temporary user-supplied credentials, 2026-10-09)

```text
12/12 checks PASS  (auth, search, details, image download+validate for both
providers; broken-IGDB -> RAWG fallback; local ranking; rate budgets; clean report)
budget used: igdb 5/1500 per day, rawg 4/250 per day, images 2/1200 per day
report: artifacts/live/live_api_report.md  (scanned clean of credentials)
```

## Blockers

None. Historical sandbox-setup notes (not project dependencies): apt-installed
`libgl1 libegl1 libxkbcommon0 libfontconfig1 xvfb fonts-dejavu-core` so Qt could
render headless.

## Honesty rules in force

- Importing a module or passing a unit test is **not** a GUI test.
- A Linux offscreen render is **not** a Windows 7 test.
- `NOT TESTED` never becomes `PASS` without a run on the stated environment.
- Missing performance numbers read `NOT MEASURED`, never an estimate.

---

## Field report — Windows 7 SP1 x64, packaged build (2026-10-10, user-run)

Evidence supplied by the user from the Dell (diagnostics shareable report +
screenshot). Recorded verbatim where it matters:

| Item | Result | Evidence |
|---|---|---|
| Frozen build launches on Win7 | **PASS** | `packaged build: True`, window rendered |
| Python 3.8 runtime | **PASS** | `python 3.8.20`, app ran (static gate confirmed by a real 3.8 interpreter) |
| Dark theme on Win7 | **PASS** | screenshot: first-run dialog, Settings, Diagnostics all dark |
| DPAPI credential store | **PASS** | `backend dpapi`, `encrypted True`, keys present after saving from Settings |
| Providers + rate limiting in real use | **PASS** | `configured ['igdb','rawg']`; budgets advancing (igdb 1/1500 day, rawg 1/250 day) |
| Fresh-install database creation | **FAIL → fixed in 0.1.1** | "No database path is configured" from first-run; Settings add-root silent |
| Process memory measurement | **FAIL → hardened in 0.1.1** | `GetProcessMemoryInfo failed`; probe now tries psapi + K32/kernel32 and records the Win32 error code |

Bugs found here are exactly why M12 exists: both were invisible to the Linux
suite (fresh-install state and Win7 ctypes behaviour) and both now have
regression tests (`tests/test_gui.py`, "fresh-install regressions").

**Open items for the next field run:** repeat first-run and Settings add-root on
0.1.1; send a fresh diagnostics report so the memory row can move off
`not measured`; then complete §3–§6 of `docs/WINDOWS7_LIVE_TEST.md`.

---

## Field report #2 — Windows 7, source + packaged (2026-10-10, user-run)

What the user confirmed working after 0.1.1: first-run setup, folder discovery,
adding games through the IGDB API ("api działa bardzo dobrze"), collection
browsing ("działa fajnie i szybko").

Newly reported and fixed in **0.1.2**:

| Report | Diagnosis | Fix |
|---|---|---|
| Crash dialog on Manager → Edit content types | missing `QCheckBox` import in `content_types.py` | import + regression test |
| "Edit content types" dead on unmatched folders | status-bar-only message | confirmation dialog that opens the import flow |
| Add-game preview shows a floating "No cover" | preview never fetched the cover; centred placeholder | preview rebuilt (cover beside title) + cover fetch on selection |
| Covers only for the selected result | single on-demand thumbnail | background prefetch of all rows, rank order, limiter-paced, cancellable |
| No way to inspect a folder during import | missing action | "Open folder" button on the folder step |
| Dialogs rendered black on Win7 | QSS rule order made QDialog transparent | rule order fixed + dark-dialog regression test |

Still open on the target: re-verify the 0.1.1 memory probe (working-set row),
then `docs/WINDOWS7_LIVE_TEST.md` §3–§6 in full.
