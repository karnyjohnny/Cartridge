# QWEN.md — persistent working memory

_This file is the recovery point. If a session dies, read this first — not the chat log._
_Last updated: 2026-10-09_

## Purpose

**Cartridge** — a local, dark-mode desktop game collection manager for Windows 7 SP1 x64.
It catalogues existing game folders, fetches metadata (IGDB primary, RAWG fallback),
stores artwork locally next to each game, and never moves/renames/deletes user files.

## Hard constraints (do not renegotiate)

| Constraint | Value |
|---|---|
| OS target | Windows 7 SP1 x64 (Dell Latitude E5500: Core 2 Duo, 2 GB RAM, GMA 4500MHD, 1280×800, HDD) |
| Python | 3.8.x (3.8.20 preferred). Source must stay 3.8-compatible — `vermin` enforces it |
| GUI | PyQt5 5.15.x (Qt runtime pinned to 5.15.2). No PyQt6/PySide6/Electron/web frontend |
| HTTP | `httpx` only. Never `requests`/`urllib`/`aiohttp` for app traffic |
| Storage | stdlib `sqlite3`. Artwork on disk, not as DB blobs |
| Forbidden | Visual Studio/Build Tools, .NET, MSBuild, Node, Java, Python > 3.8, Win10-only APIs |
| Safety | No moving/renaming/deleting game files. No executing discovered files. No secrets in repo/logs/exports |

## Verified stack

**Development sandbox (where the automated suite runs):**

```text
OS               Debian GNU/Linux 12 (bookworm), x86_64, headless (Qt offscreen plugin)
Python           3.11.2   <-- NOT the 3.8 target; source is kept 3.8-compatible and
                             verified statically with vermin (see scripts/check_py38.py)
PyQt5            5.15.11
Qt runtime       5.15.2   (qVersion()); note QT_VERSION_STR reports 5.15.14, which is
                          the version PyQt5 was *compiled against*, not the loaded lib
httpx            0.28.1
sqlite3 lib      3.40.1
```

**Target pins for the Dell (in `requirements.txt`, PyPI `Requires-Python` verified):**

```text
PyQt5==5.15.11  PyQt5-Qt5==5.15.2  PyQt5-sip==12.15.0
httpx==0.28.1   httpcore==1.0.9    h11==0.16.0
anyio==4.6.2    sniffio==1.3.1     idna==3.10   certifi==2025.1.31
```

## Repository layout

```text
run.py                     single entry point (python run.py [--selftest])
cartridge/
  paths.py                 path normalization, identity keys, folder-state classification
  text.py                  title normalization, folding, match scoring, ranking helpers
  config.py                settings load/save, roots, credential plumbing
  db/                      schema.py (DDL+migrations), connection.py, repository.py
  scan/                    discovery.py, content_types.py, folder_size.py
  providers/               base.py (httpx), igdb.py, rawg.py, mapping.py, ranking.py
  artwork/                 store.py, downloader.py, images.py, cache.py
  core/                    models.py, filtering.py, workers.py, redaction.py
  ui/                      theme.py, main_window.py, views/, dialogs/, widgets/
  diagnostics.py           sanitized diagnostics report
scripts/
  smoke_test.py            M1 compatibility proof (9 checks, honest statuses)
  check_py38.py            vermin gate: proves the tree is Python 3.8 compatible
  live_api_check.py        opt-in live IGDB/RAWG verification (needs credentials)
  gen_demo_data.py         synthetic collection for perf tests (clearly fake data)
  secret_scan.py           scans tracked files + artifacts for credential leaks
tests/                     offline suite; test_live_providers.py is opt-in
docs/                      STATUS, DECISIONS, TEST_REPORT, PERFORMANCE_REPORT, WINDOWS7_LIVE_TEST
packaging/                 PyInstaller spec + Windows 7 build notes
```

## Commands

```bash
python -m venv .venv && .venv\Scripts\activate        # Windows
python -m pip install -r requirements.txt -r requirements-dev.txt

python run.py                                          # launch the app
python run.py --selftest                               # construct UI headlessly, exit 0/1
python -m pytest tests/ -q                             # automated suite (offline, no creds)
python scripts/smoke_test.py                           # M1 compatibility proof
python scripts/smoke_test.py --network                 # ... plus a live HTTPS probe
python scripts/check_py38.py                           # Python 3.8 compatibility gate
python scripts/secret_scan.py                          # credential leak scan

# opt-in live provider tests (Windows PowerShell example; credentials from env only)
$env:CARTRIDGE_LIVE="1"; $env:CARTRIDGE_IGDB_CLIENT_ID="..."; python -m pytest tests/test_live_providers.py -q
```

## Current milestone

M0–M10 and M13 are **PASS in the development sandbox**; M11 (packaging) and M12
(Windows 7 live verification) are **NOT TESTED** with complete procedures written
(`packaging/BUILD_WINDOWS7.md`, `docs/WINDOWS7_LIVE_TEST.md`). Authoritative table:
`docs/STATUS.md`.

## Verified evidence (2026-10-09)

```text
offline suite        561 passed, 8 skipped (opt-in live)
GUI (offscreen)      37 tests + run.py --selftest OK (97% dark pixels)
smoke M1             7 PASS / 2 NOT TESTED;  --network: 8 PASS / 1 NOT TESTED
live IGDB+RAWG       12/12 PASS (auth 642 ms, search, details, real image downloads,
                     broken-IGDB -> RAWG fallback, ranking, budgets, clean report)
live pytest          8/8 with CARTRIDGE_LIVE=1
py38 gate            PASS (vermin --target=3.8- over 79 files + forbidden scan + ast)
secret scan          PASS, including with the real credentials loaded in the env
performance          1,000 synthetic games: query-all 34.8 ms, search 36.1 ms,
                     5 scrolls 45.1 ms, 22 card widgets, 61.4 MB browsing RSS
screenshots          artifacts/screenshots/*.png (real grabs + provenance JSON)
```

## Known defects / limitations

- Windows 7 / Python 3.8 execution and the frozen build are unverified (M11/M12).
- DPAPI store round-trip is Windows-only code and is NOT TESTED here.
- Cross-language title matching depends on provider alternative_names.
- Discovery is one level deep by design (DECISIONS D-007).
- Sandbox numbers are shapes, not budgets; the Dell decides the budget.

## Next concrete task

On the Dell: run `docs/WINDOWS7_LIVE_TEST.md` §1–§6, fill the DELL rows of
`docs/PERFORMANCE_REPORT.md` and `docs/STATUS.md`, then build and launch the
frozen bundle per `packaging/BUILD_WINDOWS7.md` §"Verify on the target".
