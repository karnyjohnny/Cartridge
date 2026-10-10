# TEST_REPORT.md

Commands, environments and results. Automated, local-GUI, live-API and
Windows-7 rows are kept separate on purpose (brief section 17): they are not the
same kind of evidence and must never be merged into one number.

**Run on 2026-10-09. App version 0.1.0.**

| Environment | OS | Python | Qt (runtime) | Notes |
|---|---|---|---|---|
| `SANDBOX` | Debian 12, headless | 3.11.2 | 5.15.2 | agent container; Qt `offscreen` plugin |
| `DELL` | Windows 7 SP1 x64 | 3.8.20 (intended) | 5.15.2 (intended) | **not available to the agent** |

---

## 1. Automated suite (offline, no credentials, no network)

Command: `python -m pytest tests -q`

```text
561 passed, 8 skipped in 67.9 s
```

The 8 skipped tests are the opt-in live provider tests
(`tests/test_live_providers.py`), skipped because `CARTRIDGE_LIVE` was unset.

| Area | File | Tests | Result |
|---|---|---|---|
| path identity / folder state | `tests/test_paths.py` | 35 | PASS |
| title folding / match scoring | `tests/test_text.py` | 40 | PASS |
| credential redaction | `tests/test_redaction.py` | 20 | PASS |
| schema / migrations / repository / transactions | `tests/test_db.py` | 85 | PASS |
| httpx transport: timeouts, retries, status, downloads | `tests/test_http_base.py` | 38 | PASS |
| IGDB / RAWG / mapping / ranking / fallback | `tests/test_providers.py` | 89 | PASS |
| rate limiting policies and budgets | `tests/test_ratelimit.py` | 35 | PASS |
| image validation / hashing / thumbnails | `tests/test_artwork.py` | 34 | PASS |
| artwork store safety / download pipeline / cache | `tests/test_artwork_store.py` | 62 | PASS |
| filesystem discovery / content types / size | `tests/test_scan.py` | 57 | PASS |
| worker pool lifetimes and cancellation | `tests/test_workers.py` | 26 | PASS |
| GUI (offscreen) — window, views, dialogs | `tests/test_gui.py` | 37 | PASS |
| live-captured fixture mapping (offline) | `tests/test_live_fixtures.py` | 11 | PASS (2 skipped when no capture) |

## 2. Local GUI verification (SANDBOX, `offscreen` plugin)

* `python run.py --selftest` → `SELFTEST OK: window built, 5 views navigated,
  theme rendered (97% dark pixels), database opened` — exit 0.
* `scripts/smoke_test.py` → 7 PASS / 2 NOT TESTED (`httpx_https` opt-in,
  `packaging` Windows-only). Report: `artifacts/smoke/smoke_report.md`.
* `scripts/smoke_test.py --network` → 8 PASS / 1 NOT TESTED (`packaging`).
  Report: `artifacts/smoke/smoke_report_network.md`.
* Real renders captured with `scripts/screenshot.py` (every image is a
  `QWidget.grab()` of the running app): `artifacts/screenshots/*.png` plus
  `screenshots.json` recording host, plugin and database for each.
* `tests/test_gui.py` drives the real widgets: navigation, debounced search,
  facet chips, selection → details, favourite toggle, list/grid switch, manager
  rescan through the worker pool, manual import end-to-end, edit overrides
  surviving a provider refresh, content-type CRUD, image viewer, first-run
  dialog. **37 PASS.**

This is *not* a Windows 7 test and is never reported as one.

## 3. Live API verification (SANDBOX network, real temporary credentials)

Command: `python scripts/live_api_check.py` (and `pytest tests/test_live_providers.py -q`
with `CARTRIDGE_LIVE=1`).

```text
credentials      PASS   IGDB + RAWG configured from environment
igdb_auth        PASS   Twitch OAuth2 token in 642 ms, expiry 86339 s
igdb_search      PASS   2 and 6 results, 688 / 558 ms
igdb_details     PASS   id 478 "The Witcher 2: Assassins of Kings" (2011), 5 screenshots
igdb_image       PASS   15,808 B JPEG 264x352 downloaded, validated, thumbnailed
rawg_search      PASS   6 results, 979 / 366 ms
rawg_details     PASS   summary present, 575 ms
rawg_image       PASS   362,037 B JPEG 1920x1080 downloaded and validated
provider_fallback PASS  broken IGDB secret -> RAWG answered (4 results)
local_ranking    PASS   reorders real results; top score 1.00
rate_limit_budget PASS  igdb 5/1500, rawg 4/250, images 2/1200 per day
report_is_clean  PASS   no credential material in the generated report
pytest live      PASS   8/8 with CARTRIDGE_LIVE=1
```

Report: `artifacts/live/live_api_report.md` (+ JSON). Sanitized captures of the
real payloads are stored as `tests/fixtures/live_*.json` and re-tested offline by
`tests/test_live_fixtures.py`.

Bugs found *only* by live testing, and fixed:
`artwork/images.py` checked the JPEG end-marker in the first 64 bytes instead of
the last (every valid cover was called "truncated"); Twitch answers a bad secret
with HTTP 400, which was reported as a generic request rejection; RAWG's list
endpoint returns no developers/publishers (hence `enrich()`).

## 4. Static compatibility gate

Command: `python scripts/check_py38.py`

```text
forbidden-construct scan : OK (79 files)
vermin --target=3.8-     : OK  (minimum required 3.8)
ast.parse 3.8 grammar    : OK
RESULT: PASS
```

This proves the *source* is Python 3.8 compatible. It does **not** prove the app
runs on 3.8 — that row stays NOT TESTED until executed on the Dell.

## 5. Secret hygiene

Command: `python scripts/secret_scan.py` (and with credentials exported, so the
known-value layer runs)

```text
scanned 103 files · 3 known secret values checked · 13 patterns
RESULT: PASS
```

Also verified: fixtures and the live report contain no credential values
(`tests/test_live_fixtures.py::test_captures_contain_no_credentials`,
`live_api_check`'s `report_is_clean`), the repository refuses credential keys
(`tests/test_db.py::test_secrets_are_refused_by_the_database`), and diagnostics
never emits them (`tests/test_gui.py::test_diagnostics_renders_without_secrets`).

## 6. Performance (SANDBOX, 1,000 synthetic games)

Command: `scripts/perf_probe.py` + a scripted render pass. Full table with
conditions: `docs/PERFORMANCE_REPORT.md`. Headline numbers: query-all 34.8 ms,
search 36.1 ms, five viewport scrolls 45.1 ms, 22 live card widgets for 1,000
rows, browsing working set 61.4 MB. **DELL: NOT MEASURED.**

## 7. Windows 7 / Python 3.8 (DELL)

**NOT TESTED.** Checklist: `docs/WINDOWS7_LIVE_TEST.md`. Nothing in this report
claims otherwise.

---

## 8. Field verification on the target (user-run, 2026-10-10)

Environment reported by the machine itself (shareable diagnostics):

```text
python     3.8.20
platform   Windows-7-6.1.7601-SP1
machine    AMD64
packaged   True (PyInstaller onedir)
```

| Check | Result | Notes |
|---|---|---|
| Frozen build starts | PASS | window + first-run dialog rendered dark |
| App runs under Python 3.8 | PASS | confirms the vermin gate with a real interpreter |
| Secret store on Windows | PASS | DPAPI backend, encrypted, keys survive a save |
| Provider config + budgets | PASS | igdb/rawg configured; counters advancing |
| First-run DB creation | FAIL | bug: path never derived → fixed in 0.1.1 + regression test |
| Settings add-root (no DB yet) | FAIL | bug: silent `AttributeError` in slot → fixed in 0.1.1, slot now reports errors (D-016) |
| Working-set measurement | FAIL | `GetProcessMemoryInfo failed` → probe hardened in 0.1.1, awaits re-measure |

This section exists because a target run that finds bugs is *more* valuable than
a sandbox run that finds none; the failures above are recorded with the same
weight as passes.
