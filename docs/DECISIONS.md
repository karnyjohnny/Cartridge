# DECISIONS.md

Only decisions that materially affect compatibility, performance, data safety, UX or
maintainability are recorded here. Format: context → options → decision → consequence.

---

## D-001 · Keep PyQt5 + Qt runtime pinned to 5.15.2

**Context.** The brief names PyQt5 5.15.x and a known-good local install. PyPI offers
PyQt5-Qt5 up to 5.15.19.

**Options.** (a) track the newest 5.15.x; (b) pin the Qt runtime at 5.15.2; (c) move to
PyQt6/PySide6 (rejected outright — PyQt6 requires Python ≥3.9 and Qt6 does not support
Windows 7 at all).

**Decision.** (b). `requirements.txt` pins `PyQt5==5.15.11`, `PyQt5-Qt5==5.15.2`,
`PyQt5-sip==12.15.0`.

**Why.** 5.15.3+ Windows binaries are built on newer MSVC toolchains and are the most
common cause of "runs on Windows 10, fails on Windows 7". The user's environment is
already known-good; a version bump buys nothing and risks the primary constraint.

**Consequence.** `QT_VERSION_STR` reports **5.15.14** (the headers PyQt5 5.15.11 was
compiled against) while `qVersion()` reports **5.15.2** (the library actually loaded).
Both are recorded by `scripts/smoke_test.py`. Anyone debugging a Win7 launch failure must
look at `qVersion()`, not `QT_VERSION_STR`.

---

## D-002 · httpx 0.28.1, with every transitive dependency pinned

**Context.** httpx 0.28.1 declares `Requires-Python >=3.8`, but its *dependencies* do not
all support 3.8 at their newest versions (verified against PyPI metadata on 2026-10-09):

```text
anyio   4.6.2  >=3.8   OK        anyio 4.15.1  >=3.10  NOT usable on 3.8
idna    3.10   >=3.6   OK        idna  3.20    >=3.9   NOT usable on 3.8
h11     0.16.0 >=3.8   OK
sniffio 1.3.1  >=3.7   OK
```

**Decision.** Pin `httpx==0.28.1` plus `httpcore==1.0.9`, `h11==0.16.0`, `anyio==4.6.2`,
`sniffio==1.3.1`, `idna==3.10`, `certifi==2025.1.31`.

**Why.** On an offline or old machine, an unpinned resolver can pick a release whose
`Requires-Python` excludes 3.8 and fail at install time — or worse, install and break at
import time. Explicit pins make the Dell install reproducible.

**Consequence.** Dependency updates are a deliberate act, each one re-checked against
`Requires-Python` and re-run through `docs/WINDOWS7_LIVE_TEST.md`.

---

## D-003 · Folder path is game identity, not title

**Context.** The same game can be titled three different ways by three providers, and the
user may rename the folder. Duplicate records for one folder are the worst failure mode
for a catalogue.

**Decision.** Every game row carries `folder_path` (canonical, case-preserved, for display)
and `folder_key` (normalized + case-folded on Windows, `UNIQUE`). All dedup, rescan
matching and re-association compare `folder_key`.

**Why.** Windows paths are case-insensitive and separator-agnostic; `E:\Gry\Wiedzmin 2`,
`e:/gry/wiedzmin 2` and `E:\Gry\Wiedzmin 2\` are one folder. 35 tests in
`tests/test_paths.py` pin this behaviour, including UNC roots and `\\?\` long-path
prefixes.

**Consequence.** A user who *moves* a folder creates an unmatched folder + an orphaned
record. That is handled explicitly (Manager → re-associate), never by auto-deleting
metadata.

---

## D-004 · Provider fields and user edits are separate columns

**Context.** Brief §8: "Do not silently overwrite user-authored titles, notes, favourites,
tags, or overrides during a provider refresh."

**Decision.** Two-column pattern for anything a user may edit: `title` / `user_title`,
`summary` / `user_summary`, plus a `user_edited` bitmask-free approach — a per-field
`*_edited_at` timestamp is avoided in favour of a simple rule: **if the user column is
non-NULL, it wins, and a refresh never writes to it.**

**Why.** Column-per-field "was this edited" flags multiply schema churn. A non-NULL user
column is self-describing, survives migrations, and makes the effective value a single
`COALESCE(user_title, title)` expression.

**Consequence.** Clearing an override requires setting the column back to NULL — exposed
in the Edit dialog as "Use provider value".

---

## D-005 · Secrets never touch SQLite; DPAPI on Windows, 0600 file elsewhere

**Context.** Brief §15 forbids secrets in source, docs, tests, fixtures, DB records, logs,
tracebacks, screenshots, exports, commit messages, Git history and release artifacts. It
also says: prefer a Windows-appropriate protected store *if it can be tested*, otherwise a
documented session-only option or a user-controlled local file — and never invent security
guarantees.

**Decision.** `cartridge/core/secret_store.py` with three backends, tried in order:

1. **Windows DPAPI** via `ctypes` (`CryptProtectData` / `CryptUnprotectData`) — no extra
   dependency, user-scoped encryption, works on Windows 7 / Python 3.8.
2. **Local file** at `%APPDATA%\Cartridge\credentials.json` (Windows) or
   `~/.config/cartridge/credentials.json` (POSIX), created with mode `0600`.
3. **Session-only** (env vars), never persisted.

The active backend is *reported in the UI* ("Stored with Windows DPAPI" vs "Stored
unencrypted in a local file — this machine does not support protected storage"), so no
guarantee is implied that was not verified.

**Why.** `keyring` would add a dependency and needs a secret service that Windows 7 lacks
in a predictable form. DPAPI is in the OS and reachable through ctypes.

**Consequence.** The DPAPI path **cannot be tested in the Linux sandbox** → it is
`NOT TESTED` with an explicit Windows 7 verification step, and the fallback path is the one
covered by automated tests. This is recorded rather than papered over.

---

## D-006 · Bounded QThreadPool (3 workers), never ad-hoc threads

**Context.** 2 CPU cores, 2 GB RAM, mechanical disk. Unbounded concurrency would thrash
the disk and could exhaust memory while downloading artwork.

**Decision.** One shared `QThreadPool` with `maxThreadCount = 3`, used for provider calls,
artwork downloads, folder scans and size measurement. Results cross back to the GUI thread
via signals only.

**Why.** 3 ≈ cores + 1, enough to overlap network latency with disk work without
contending. Qt's pool reuses threads, so there is no per-request thread creation cost.

**Consequence.** Long queues are visible: the status bar shows pending/active counts, and
every long operation has a cancel path.

---

## D-007 · No unbounded recursive scan on launch

**Context.** Brief §7: "Avoid an unbounded recursive scan through every file on every
launch." On a mechanical HDD a deep walk of a large collection can take minutes.

**Decision.** Discovery reads **immediate children** of each configured root only
(`os.scandir`, one level). Content-type detection uses a *shallow* listing of the child
folder (bounded to the first N entries, no recursion by default). Folder size is measured
**only on explicit user request**, is stored with the timestamp of measurement, and is
shown as `-` when never measured.

**Why.** Predictable startup, and it never presents an estimate as a measurement (§9).

**Consequence.** A game nested two levels deep is not discovered automatically. Documented
behaviour; the Manager can add a folder explicitly from anywhere on disk.

---

## D-008 · Artwork stored next to the game, validated before it becomes visible

**Context.** Brief §11. Offline browsing must work, and a truncated download must never
poison the collection.

**Decision.** Assets live in `<game folder>\_cartridge\assets\`, named
`cover.jpg` / `screenshot-01.jpg` / … plus `manifest.json` (no credentials). Downloads go
to `<name>.part`, are checked (HTTP status, `Content-Length` vs received bytes, magic-byte
format sniff, decodable by `QImage`), and only then renamed into place. The DB stores the
*relative* asset path and a content hash — never the URL as the only reference.

**Why.** Rename is atomic on NTFS, so an interrupted download leaves a `.part` file, not a
half-written cover. Relative paths keep the collection portable if the user moves the root.

**Consequence.** The app writes inside the user's game folders — but only ever inside
`_cartridge\`, which is excluded from discovery and explained in Settings.

---

## D-009 · Search matching: local scoring, never auto-association

**Context.** Brief §10: provider ordering may not match intent; never auto-associate a
weak match.

**Decision.** `cartridge/text.py` defines one normalization (accents, case, punctuation,
edition markers, roman numerals → arabic, trailing language codes) and one scorer combining
exact/key/alias/token-containment/similarity signals, with two deliberate guards:

- **series-sibling guard** — differing numerals cap the score at 0.32, so "Fallout 2" vs
  "Fallout 3" can never look like a match;
- **similarity is not trusted alone** — `difflib` rates "Diablo II" vs "The Witcher 2" at
  0.53 on letter overlap alone, so raw similarity only lifts a score that token evidence
  already supports.

Auto-import requires score ≥ 0.97 **and** explicit user confirmation of the selected row.
Below that the dialog simply preselects the best candidate.

**Why.** A wrong association is expensive to notice and cheap to prevent.

**Consequence.** Cross-language matches ("Wiedzmin 2" → "The Witcher 2") depend on provider
`alternative_names`. Documented as a known limitation and pinned by a test.

---

## D-010 · Theme as generated QSS + QPalette, flat surfaces only

**Context.** GMA 4500MHD with software-limited rendering; brief §6.1 forbids blur, shadows,
filters and animation.

**Decision.** `cartridge/ui/theme.py` builds one stylesheet string from a token dict, and
also installs a matching `QPalette`. `QApplication.setStyle("Fusion")` for consistent
metrics. No `QGraphicsEffect`, no `Qt.WA_TranslucentBackground`, no gradient-heavy widgets,
no `QPropertyAnimation`.

**Why.** The palette matters as much as the QSS: native dialogs (folder picker) fall back to
it, so an unpainted white dialog would betray a half-applied theme. Fusion avoids
platform-native styling surprises between Win7 and the dev sandbox.

**Consequence.** The theme is testable as text (`build_stylesheet()` is asserted in tests)
and a future light theme is a token swap, not a rewrite.

---

## D-011 · Dev sandbox is Python 3.11, so 3.8 compatibility is enforced statically

**Context.** The agent's sandbox has Python 3.11.2. Python 3.8 cannot be installed here
(no 3.8 interpreter available, and building one needs a compiler toolchain the brief
forbids depending on).

**Decision.** Write 3.8-compatible code and prove it two ways: `scripts/check_py38.py`
runs `vermin --versions=3.8-` over the tree and fails the build on any newer-version
construct; plus a hand-maintained forbidden-API list (e.g. `str.removeprefix`,
`functools.cache`, `dict |`, PEP 604 unions, `asyncio.to_thread`, `hashlib.file_digest`).

**Why.** Running the suite on 3.11 does not prove 3.8 compatibility, and pretending
otherwise would violate the honesty rules in §17.

**Consequence.** The "runs on Python 3.8" claim stays `NOT TESTED` until executed on the
Dell. Recorded in `docs/WINDOWS7_LIVE_TEST.md` §2.

---

## D-012 · Worker pool object lifetimes (four separate segfaults, one design)

**Context.** `QRunnable` + `QObject` signals across threads is the standard PyQt
background-work pattern, and it crashes the interpreter rather than raising a
Python exception when a lifetime is wrong. Four distinct faults were found and
fixed while building `cartridge/core/workers.py`; they are recorded together
because the correct design only makes sense as a whole.

**Fault 1 — `setAutoDelete(True)` while Python held the runnable.**
`cancel()` needed the runnable after `run()` returned, but Qt had already deleted
it. Use-after-free.

**Fault 2 — `setAutoDelete(False)` as the "fix".** Worse: Python then owned the
runnable, and dropping the last reference while `QThreadPool` was still inside its
post-`run()` cleanup freed memory another thread was using.

**Fault 3 — a lambda capturing `self`, connected to a signal on an object the pool
owned.** That creates a reference cycle (`pool → handle → signals → connection →
lambda → pool`) breakable only by the garbage collector. When the GC ran during a
signal emission, the receiver was freed mid-call.

**Fault 4 — slot arity mismatch.** A one-argument lambda connected to
`failed(str, str)` made Qt deliver two arguments into one parameter.

**Decision.**

* The runnable is created with **`setAutoDelete(True)`** — `QThreadPool` owns and
  deletes it, and Python never holds the last reference. `submit()` returns a
  **`TaskHandle`**, a pure-Python object with no C++ counterpart, so UI code can
  hold, drop or ignore it without affecting any lifetime.
* `WorkerSignals` is **parentless and pool-owned**. Parenting it to the runnable
  would trip Qt's "cannot create children for a parent in a different thread"
  rule, since the runnable dies on a pool thread.
* Bookkeeping slots capture a **`weakref` to the pool**, never `self`, and every
  connection is recorded in `WorkerPool._connections` so the slot outlives the
  emission. No cycle, so no GC-during-emission window.
* Slot signatures match their signals exactly.

**Cancellation semantics** (a separate decision made while debugging): a task that
returns `None` after cancellation reports `failed("cancelled")`; a task that
stopped at a checkpoint and returned a real partial result reports `finished` with
that payload. Discarding partial scan results would throw away work the user can
still use.

**Keyword plumbing:** the pool's own label is passed as `task_name=` and popped in
`Worker.__init__`. Passing `name=` collided with callables that take their own
`name` argument; passing it positionally made it the task's first argument.

**Consequence.** `WorkerPool` is safe to use from any UI code path, and the crash
class is closed by construction rather than by careful calling. Because these were
segfaults, they are covered by tests that would have died loudly
(`tests/test_workers.py`, 26 tests) — including a 50-task burst that asserts the
thread count never exceeds the pool width.

---

## D-013 · `WA_StyledBackground` on every custom container

**Context.** The first real render of the main window came back with a white title
bar, nav rail and filter column while the grid and details pane were correctly
dark. Root cause: Qt does not paint a stylesheet background on a *user subclass of
QWidget* unless `Qt.WA_StyledBackground` is set; Qt's own classes (QScrollArea,
QLabel, QStatusBar) get it automatically. The offscreen grab showed those widgets
as fully transparent.

**Decision.** Every custom container that owns a QSS background calls
`setAttribute(Qt.WA_StyledBackground, True)` in its constructor, and the grid
canvas and root surface have explicit QSS rules. `tests/test_gui.py`
asserts >85% dark pixels on a rendered window so a regression cannot hide.

**Consequence.** The dark theme is now structural, not accidental, and the rule is
documented where a future widget author will trip over it.

---

## D-014 · Probes never create; read-only fields never receive `setText`

**Context.** Two Qt/filesystem behaviours produced crashes or side effects rather
than errors:

1. `is_writable_location()` created the directory it was probing. Because the
   Settings/first-run flow probes while the user is still choosing a folder,
   typing a path littered the disk with unconfirmed directories.
2. `QLineEdit.setText()` on a **read-only** line edit aborts the process in
   Qt ≥ 5.15 (`Q_ASSERT`). The first-run dialog and the add-game folder field are
   read-only by design (the folder comes from a browse button), so any
   programmatic write — including the one inside `_on_continue` — was a latent
   crash.

**Decision.** Writability probes are pure by default; creating requires an
explicit `create=True`, and for not-yet-existing targets the probe walks to the
nearest existing ancestor (`probe_writable_for_creation`). Read-only fields are
written through a `_set_root_text`-style helper that flips the flag around the
write. A `NameError` inside a Qt slot aborts the interpreter, so configuration
helpers called from slots are covered by GUI tests.

**Consequence.** Choosing a root has no side effects until the user confirms, and
the two dialogs are crash-free on the pinned Qt.

---

## D-015 · The browser recycles a viewport-sized card pool

**Context.** Brief §12 warns against rebuilding every card per keystroke and
requires a 1,000-record collection to stay practical on a mechanical-disk machine.
A delegate-painted `QListView` would be marginally faster but makes the reference
card layout (cover + title + year + score + developer + badges) much harder to
keep legible and accessible.

**Decision.** `CardGrid` keeps a pool of card widgets sized to the viewport and
re-populates them on scroll; the database does all filtering and sorting in one
parameterized query; search is debounced at 220 ms.

**Consequence.** Measured on 1,000 synthetic games (development sandbox): 22 live
card widgets, ~9 ms per viewport re-render, 52.6 ms for query + populate + paint.
Widget count and memory no longer scale with collection size.

---

## D-016 · Exceptions in Qt slots must become dialogs, not silence

**Context.** On the first real Windows 7 run, clicking "Add root folder…" produced
*no reaction at all*. The cause was an `AttributeError` raised inside a slot: a
windowed PyQt build has no console, so the traceback went to an stderr nobody can
see, and Qt swallowed the failure. A silent no-op is the worst possible error
message for a user who just made a decision.

**Decision.** Every slot that can fail on a fresh install wraps its body in
`try/except` and shows a `QMessageBox` with the redacted message
(`SettingsView._on_add_root` is the reference implementation). The inner worker
(`_add_root_chosen`) stays exception-propagating so tests can assert on it
directly, while the user-facing entry point never leaks.

**Consequence.** Failures on the target machine are reportable: the user sees text
they can screenshot, instead of "nothing happened". Regression test:
`tests/test_gui.py::test_settings_add_root_surfaces_errors_instead_of_silence`.
