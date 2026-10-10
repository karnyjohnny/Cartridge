r"""Optional, cancellable folder-size measurement.

Brief section 9: folder size may be measured on demand because a deep scan is
expensive on a mechanical disk, and the value must be reported as measured (with
its timestamp) — never estimated.

Consequences of that rule, all implemented here:

* measurement is **explicit**, never automatic during discovery;
* the walk is **cancellable** and reports progress, so the UI can offer a way out
  of a five-minute scan;
* a partial result is labelled ``complete=False`` so the UI can say
  "measured 4,213 of an unknown number of files" instead of showing a number that
  looks final;
* per-directory permission errors are collected, not raised — one locked folder
  inside a game does not abort the measurement.

Symlinks are never followed: on Windows a junction pointing at ``C:\`` would
otherwise turn one game folder into a scan of the whole disk.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from cartridge.core.clock import monotonic, now_iso

DEFAULT_PROGRESS_EVERY = 256


@dataclass
class SizeResult(object):
    """The outcome of one measurement."""

    path: str = ""
    size_bytes: int = 0
    files: int = 0
    directories: int = 0
    complete: bool = True
    cancelled: bool = False
    duration_ms: float = 0.0
    measured_at: Optional[str] = None
    errors: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.complete and not self.cancelled

    def describe(self) -> str:
        from cartridge.text import human_size

        base = "%s in %d files" % (human_size(self.size_bytes), self.files)
        if self.cancelled:
            return base + " (cancelled - partial)"
        if not self.complete:
            return base + " (partial: some folders were unreadable)"
        return base


def measure_folder_size(
    path: str,
    cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int], None]] = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
    max_files: Optional[int] = None,
) -> SizeResult:
    """Walk ``path`` and total up file sizes.

    ``cancel()`` is polled between files and should return True to stop.
    ``progress(files, bytes)`` is called every ``progress_every`` files.
    ``max_files`` caps the work; hitting it marks the result incomplete rather
    than pretending the number is final.
    """
    result = SizeResult(path=path)
    if not path or not os.path.exists(path):
        result.complete = False
        result.errors.append("path does not exist")
        return result
    if os.path.isfile(path):
        try:
            result.size_bytes = os.path.getsize(path)
            result.files = 1
        except OSError as exc:
            result.complete = False
            result.errors.append(str(exc))
        result.measured_at = now_iso()
        return result

    started = monotonic()
    since_progress = 0

    def note_progress(force: bool = False) -> None:
        nonlocal since_progress
        since_progress += 1
        if progress is not None and (force or since_progress >= progress_every):
            since_progress = 0
            try:
                progress(result.files, result.size_bytes)
            except Exception:
                # A broken progress callback must not lose the measurement.
                pass

    try:
        walker = os.walk(path, topdown=True, onerror=None, followlinks=False)
        for current, dirnames, filenames in walker:
            if cancel is not None and cancel():
                result.cancelled = True
                result.complete = False
                break
            # Skip reparse points explicitly: os.walk(followlinks=False) does not
            # follow symlinked *directories*, but a Windows junction can still
            # appear as a directory and hide a cycle.
            keep = []
            for name in dirnames:
                full = os.path.join(current, name)
                try:
                    if os.path.islink(full):
                        continue
                    if os.name == "nt" and _is_reparse_point(full):
                        continue
                except OSError:
                    continue
                keep.append(name)
            dirnames[:] = keep
            result.directories += len(dirnames)

            for name in filenames:
                if cancel is not None and cancel():
                    result.cancelled = True
                    result.complete = False
                    break
                full = os.path.join(current, name)
                try:
                    if os.path.islink(full):
                        continue
                    result.size_bytes += os.path.getsize(full)
                    result.files += 1
                except OSError as exc:
                    # One unreadable file does not invalidate the rest, but the
                    # total is no longer exact, so say so.
                    result.complete = False
                    if len(result.errors) < 20:
                        result.errors.append("%s: %s" % (name, exc))
                note_progress()
                if max_files is not None and result.files >= max_files:
                    result.complete = False
                    result.errors.append(
                        "stopped after %d files (configured limit)" % result.files
                    )
                    break
            if result.cancelled or (max_files is not None and result.files >= max_files):
                break
    except OSError as exc:
        result.complete = False
        result.errors.append(str(exc))

    note_progress(force=True)
    result.duration_ms = round((monotonic() - started) * 1000.0, 1)
    result.measured_at = now_iso()
    return result


def _is_reparse_point(path: str) -> bool:
    """Windows junction/symlink detection without pywin32."""
    try:
        import stat

        mode = os.lstat(path).st_mode
        # FILE_ATTRIBUTE_REPARSE_POINT surfaces as S_IFLNK or via the mode bits
        # on modern Python; check both to be safe.
        if stat.S_ISLNK(mode):
            return True
        return bool(mode & 0x40000000)  # FILE_ATTRIBUTE_REPARSE_POINT
    except (OSError, AttributeError, ValueError):
        return False


def measure_many(
    paths: List[str],
    cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[str, SizeResult], None]] = None,
) -> List[SizeResult]:
    """Measure several folders, reporting each as it finishes."""
    out: List[SizeResult] = []
    for path in paths or ():
        if cancel is not None and cancel():
            break
        result = measure_folder_size(path, cancel=cancel)
        out.append(result)
        if progress is not None:
            try:
                progress(path, result)
            except Exception:
                pass
    return out


def quick_entry_count(path: str, limit: int = 5000) -> int:
    """Cheap directory-entry count for the Manager's "looks big" hint.

    Bounded and never recursive; returns ``limit`` when the cap is hit so the UI
    can show "5000+" instead of a false exact number.
    """
    count = 0
    try:
        with os.scandir(path) as iterator:
            for _entry in iterator:
                count += 1
                if count >= limit:
                    return count
    except OSError:
        return count
    return count
