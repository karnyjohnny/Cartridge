# Cartridge

A local-first desktop game collection manager for **Windows 7 SP1 x64**, built with
**Python 3.8 + PyQt5 + httpx + SQLite**. It catalogues the game folders you already
have, fetches metadata from IGDB (RAWG as fallback), stores artwork next to each
game, and never moves, renames or deletes your files.

> **Status in one line:** implemented and verified on a Linux development sandbox
> (561 automated tests, live IGDB/RAWG verification with 12/12 checks passing);
> **not yet executed on Windows 7 / Python 3.8** — that checklist is in
> [`docs/WINDOWS7_LIVE_TEST.md`](docs/WINDOWS7_LIVE_TEST.md) and every affected
> claim in [`docs/STATUS.md`](docs/STATUS.md) is marked `NOT TESTED`, not `PASS`.

---

## What it does

* **Collection Browser** — dark, compact cover grid (or list) with debounced
  search across titles/aliases/people/genres/tags, facet filters (content type,
  genre, decade), sorts with unknown values always last, and a details pane with
  cover, screenshots, release data, provider attribution and folder state.
* **Manager** — the collection's health as a table: unmatched folders, incomplete
  metadata, missing artwork, missing folders and unavailable drives are five
  *separate* states. Rescan runs on a background thread; vanished folders are
  marked, never deleted.
* **Import** — the 11-step flow from the approved design reference: folder →
  scan summary and suggested content types → IGDB/RAWG search → ranked results
  with per-row confidence → artwork selection → import. A weak match always asks
  for confirmation; manual entry is always available.
* **Settings** — roots, masked IGDB/RAWG credentials with an explicit
  "Test connection", artwork options, database location shown in plain words.
* **Diagnostics** — real measurements only: versions, database integrity, working
  set, latency samples, rate-limit budgets. The shareable report contains no
  paths, titles or credentials; a detailed local export is possible but warns
  first.

## Non-negotiables (and how they are enforced)

| Rule | Enforcement |
|---|---|
| Your files are never modified | discovery and import read only; writes go exclusively to `<root>\.cartridge` and `<game>\_cartridge`; tests assert the tree is byte-identical after a scan |
| Nothing runs because it was found | no auto-execution; launching a game requires an explicit user-selected executable |
| Credentials never leak | secret store (Windows DPAPI / 0600 file / session-only), repository refuses credential keys, redactor on every error path, release-time `scripts/secret_scan.py` |
| Providers are treated as untrusted input | defensive mapping, typed errors, byte caps, magic-byte + truncation + decode validation before any file is accepted |
| Rate limits are respected | token bucket + per-minute/day budgets below published limits; `Retry-After` honoured; long waits become user-visible refusals, not freezes |
| The GUI never blocks | one bounded worker pool (3 threads), cooperative cancellation, progress signals |
| Python 3.8 compatibility | `scripts/check_py38.py` = vermin `--target=3.8-` + forbidden-construct scan + 3.8-grammar parse |

## Supported environment

```text
Windows 7 SP1 x64            (primary target; NOT yet verified by the developers)
Python 3.8.x x64             (3.8.20 preferred; source is 3.8-clean, runtime unverified)
PyQt5 5.15.11 + Qt 5.15.2    (Qt runtime pinned deliberately - see docs/DECISIONS.md D-001)
httpx 0.28.1                 (all transitive pins verified against Requires-Python)
SQLite via stdlib sqlite3
No Visual Studio, .NET, Node, Java or browser runtime required.
```

Development also works on Linux/macOS (the automated suite and the offscreen GUI
tests run there).

## First-time setup and run

```bash
python -m venv .venv
.venv\Scripts\activate                      # Windows
# source .venv/bin/activate                 # Linux/macOS
python -m pip install -r requirements.txt

python run.py                               # launch
python run.py --selftest                    # headless build+render check, exit 0/1
python run.py --root "E:\Gry"               # open a specific root
python run.py --offline                     # never contact providers
```

First launch asks for your games root and states exactly which directories it
will create before creating anything.

## Tests, live tests, packaging

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q                   # offline suite (no network, no creds)

# opt-in live provider tests (real internet, real credentials from the env only)
set CARTRIDGE_LIVE=1                        # Windows; export on POSIX
set CARTRIDGE_IGDB_CLIENT_ID=...
set CARTRIDGE_IGDB_CLIENT_SECRET=...
set CARTRIDGE_RAWG_API_KEY=...
python -m pytest tests/test_live_providers.py -q
python scripts\live_api_check.py            # full live verification + sanitized report

python scripts\smoke_test.py                # M1 compatibility proof
python scripts\check_py38.py                # Python 3.8 static gate
python scripts\secret_scan.py               # credential leak scan

pyinstaller packaging\cartridge.spec        # see packaging/BUILD_WINDOWS7.md first
```

## IGDB / Twitch and RAWG configuration

IGDB requires a Twitch application (client id + client secret); Cartridge performs
the OAuth2 client-credentials flow itself and caches the token in memory only.
RAWG needs an API key and is used as an automatic fallback. Enter both in
**Settings → Metadata providers**, or export the three `CARTRIDGE_*` environment
variables for a session. Where they are stored, and how honestly that is reported,
is described in `docs/DECISIONS.md` (D-005).

## Where your data lives

```text
<games root>\.cartridge\collection.db        the catalogue (visible in Settings)
<game folder>\_cartridge\assets\             cover + screenshots + manifest.json
<game folder>\_cartridge\assets\thumbs\      display-size thumbnails (disposable)
%APPDATA%\Cartridge\                         credential store (Windows)
```

Backup = copy the `.db` (Settings has a button that uses SQLite's online backup
API) + the `_cartridge` folders travel with the games.

## Screenshots

`artifacts/screenshots/*.png` are genuine renders of the running application
(`QWidget.grab()`), each accompanied by `screenshots.json` recording the host, Qt
platform plugin and database. They are **not** the HTML design reference and not
Windows 7 captures; the file names say which plugin produced them.

## Known limitations

* Windows 7 / Python 3.8 execution is unverified (checklist provided).
* No frozen executable has been built or launched yet.
* Cross-language title matching ("Wiedzmin 2" → "The Witcher 2") relies on the
  provider's alternative names; local scoring deliberately refuses to guess.
* Folder discovery is one level deep by design; deeper nesting must be added
  explicitly through the Manager.
* Folder size is measured only on request and is labelled with its measurement
  time; unmeasured folders show "not measured", never an estimate.

## License

MIT — see [LICENSE](LICENSE).
