# Building a Windows 7 package (milestone M11)

**Status: NOT TESTED.** No frozen build has been produced or launched on Windows 7.
Everything below is the procedure; the results must be filled in afterwards.

## Why this is a separate document

Brief section 3 makes packaging a *verified* activity, not an assumption:
"Building an executable on a newer Windows host does not by itself prove that the
executable runs on Windows 7." The common failure modes are the bootloader's CRT
dependency, a Qt build compiled on a newer toolchain, and UPX/AV interactions —
none of which are visible until the exe is launched on the target.

## Prerequisites on the build machine

```text
Windows 7 SP1 x64 (preferred) or a Win7-class VM
Python 3.8.20 x64 from python.org
requirements.txt installed exactly as pinned
PyInstaller pinned in requirements-dev.txt (see note below)
```

PyInstaller version selection rule: choose the newest release whose wheel metadata
still declares Python 3.8 support **and** whose bootloader is known to start on
Windows 7. Record the exact version in `requirements-dev.txt` and in
`docs/STATUS.md` M11 before building. Do not let pip resolve a newer one.

## Build

```bat
cd cartridge
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt -r requirements-dev.txt
python scripts\check_py38.py            REM must print RESULT: PASS
python -m pytest tests -q               REM must pass with no live tests
pyinstaller packaging\cartridge.spec --noconfirm --clean
```

Output: `dist\Cartridge\Cartridge.exe` plus its folder (onedir build; see the spec
file for why onedir rather than onefile on a mechanical disk).

## Verify on the target (mandatory)

1. Copy the whole `dist\Cartridge` folder to the Dell.
2. Double-click `Cartridge.exe`. Expected: the window opens on the dark theme at
   1280×800 within a few seconds.
3. Run the frozen self-test from a command prompt:
   `Cartridge.exe --selftest` → must print `SELFTEST OK` and exit 0.
4. Execute `docs/WINDOWS7_LIVE_TEST.md` §4 (frozen-build checklist).
5. Confirm no console window appears (the build is windowed) and that the first-run
   dialog offers a root folder.
6. Record in `docs/STATUS.md` M11: PyInstaller version, build host OS, whether the
   launch succeeded, and the self-test output.

## Secret hygiene for release artifacts (brief section 15)

Before publishing anything:

```bat
python scripts\secret_scan.py
```

The scanner checks tracked files, untracked-but-not-ignored files, the `dist/`
folder and `artifacts/` for credential-shaped values and for the specific secret
values held in the environment. A frozen bundle must not contain credentials: the
app never embeds them (they come from the environment or the user's secret store
at runtime), but the scan proves it rather than asserting it.

If a credential is ever found in an artifact: stop distributing it, remove it, and
tell the user to revoke/rotate the credential. Do not paste the secret into any
report.

## Known risks (disclosed, not hidden)

* If PyInstaller's bootloader for the chosen version does not start on Win7, the
  fallback is the documented source run (`python run.py`), which needs only the
  Python 3.8 installer — no compiler, no Visual Studio.
* Qt 5.15.2's Windows binaries are the pinned runtime precisely because later
  5.15.x builds are compiled on newer toolchains; do not "fix" a Qt DLL mismatch by
  upgrading Qt.
