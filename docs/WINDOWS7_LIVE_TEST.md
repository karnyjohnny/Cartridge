# WINDOWS7_LIVE_TEST.md — repeatable checklist for the Dell Latitude E5500

Everything in this file must be executed **on the target machine**. The agent's
sandbox is Linux/Python 3.11 and cannot produce this evidence; until these steps
are run, the corresponding rows in `docs/STATUS.md` stay `NOT TESTED`, never
`PASS`.

Copy results into this file (or into a dated note next to it) verbatim, including
failures. A failure with details is more useful than a blank.

**Prerequisites**

```text
Windows 7 SP1 x64
Python 3.8.20 x64 installed for the current user or all users
this repository (source checkout is enough - no compiler is required)
```

---

## §1 Compatibility proof (M1-W)

```bat
cd cartridge
python --version                      REM expect: Python 3.8.20
python -m pip install -r requirements.txt -r requirements-dev.txt
python scripts\smoke_test.py --network
```

Record the whole table. Expected: every row PASS except `packaging` (until §4)
and `app_entry_point` (PASS once run.py works). Paste versions:

```text
Python:      ____
PyQt5:       ____   (expect 5.15.11)
Qt runtime:  ____   (expect 5.15.2 - from qVersion(), NOT QT_VERSION_STR)
httpx:       ____   (expect 0.28.1)
SQLite lib:  ____
```

## §2 Python 3.8 runtime (M12-a)

```bat
python run.py --selftest
```

Expected: `SELFTEST OK ...` and exit code 0. This is the real 3.8 check that the
static gate (vermin) can only approximate. If a `SyntaxError` or `ImportError`
appears, note the exact message — it means a 3.9+ construct slipped through.

## §3 Interactive GUI walk-through (M12-b)

Run `python run.py`. At 1280×800, verify and tick:

- [ ] window opens dark, no white flash, no light dialog anywhere
- [ ] first-run dialog appears; choose the games root; it states where the
      database will be created before anything is written
- [ ] Collection: covers appear; scrolling 100+ games stays responsive
- [ ] search box filters as you type (small delay is the debounce, by design)
- [ ] facet chips (content type / genre / decade) filter correctly
- [ ] sort by year and by score puts unknown values last in both directions
- [ ] grid ↔ list toggle works; selection follows into the details pane
- [ ] details pane shows folder path, size ("not measured" until you measure it),
      provider attribution, description, screenshots
- [ ] `Ctrl+F` focuses search, `F5` rescans, `Esc` clears filters
- [ ] Favourites view shows only favourites; toggling a star updates both views
- [ ] Manager: unmatched folders, missing artwork, missing folders and
      unavailable drives are listed as *separate* states
- [ ] Manager → Add game: search IGDB, results show provider + confidence; a
      weak match asks for confirmation; "Enter details manually" works offline
- [ ] Settings: credential fields are masked; "Test connection" reports latency
        and, for a wrong secret, says the credentials were rejected (not "HTTP 400")
- [ ] Settings: the storage backend is stated ("DPAPI" expected on Win7)
- [ ] Diagnostics: shows versions, DB integrity, working set, latency samples;
      "Save shareable" produces a file with no paths and no titles
- [ ] unplug / rename a game folder, rescan: the record shows "folder missing"
      and its metadata survives; deleting a record requires an explicit confirm
- [ ] close and relaunch: collection, favourites, edits and the last view return

## §4 Frozen build (M11)

Follow `packaging/BUILD_WINDOWS7.md`. Then:

- [ ] `dist\Cartridge\Cartridge.exe` launches from a double-click
- [ ] `Cartridge.exe --selftest` prints `SELFTEST OK`, exit 0
- [ ] no console window appears
- [ ] `python scripts\secret_scan.py` PASS on the build machine, and the `dist`
      folder contains no credential values

## §5 Credentials storage (DECISIONS D-005)

- [ ] enter credentials in Settings, save, restart: they are still there
- [ ] the Settings text says "Windows DPAPI"; confirm the file
      `%APPDATA%\Cartridge\secrets.bin` exists and is not readable as plain text
      (open it in Notepad: it must look like binary gibberish)

## §6 Performance (M10, the numbers that count)

```bat
python scripts\gen_demo_data.py --root C:\cartridge-perf --games 1000 --with-artwork
python scripts\perf_probe.py --db C:\cartridge-perf\.cartridge\demo-collection.db ^
       --root C:\cartridge-perf --rounds 7
```

Paste the table into `docs/PERFORMANCE_REPORT.md` under a DELL section, together
with idle CPU observed in Task Manager over 30 s of inactivity.

## §7 What to do if something fails

Record the exact text of any dialog or traceback (it is already redacted), the
step number, and whether it reproduced. Then continue with the remaining steps —
a single failing item must not stop the rest of the checklist, and never delete
work because one check failed.
