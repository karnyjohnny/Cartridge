"""Advisory content-type detection from folder contents.

Brief section 9: detection is **advisory only**. Nothing here opens an
executable, reads a registry, inspects PE headers or runs anything — it looks at
*names* in a shallow directory listing and suggests labels the user can accept,
edit or ignore. Content types are user-defined data stored in SQLite, so this
module only produces suggestions; it never creates or renames a type by itself.

The listing is bounded: a game folder can contain tens of thousands of files
(Steam depot chunks, extracted archives), and reading all of them on a mechanical
disk would make the Manager crawl. ``max_entries`` caps the work and the result
says whether the cap was hit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_MAX_ENTRIES = 200

# Names that indicate a *store or platform* install rather than a loose game.
GOG_PATTERNS = (
    re.compile(r"^goggame-.*\.info$", re.IGNORECASE),
    re.compile(r"^goggame-.*\.dll$", re.IGNORECASE),
    re.compile(r"^goggame.*\.ico$", re.IGNORECASE),
    re.compile(r"^gog(_|-)?galaxy", re.IGNORECASE),
    re.compile(r"^support$", re.IGNORECASE),      # GOG installer support dir
)
STEAM_PATTERNS = (
    re.compile(r"^steam_appid\.txt$", re.IGNORECASE),
    re.compile(r"^steam_api(64)?\.dll$", re.IGNORECASE),
    re.compile(r"^\.(steam|vdf)$", re.IGNORECASE),
    re.compile(r"^steamapps?$", re.IGNORECASE),
)
EPIC_PATTERNS = (
    re.compile(r"^\.egstore$", re.IGNORECASE),
    re.compile(r"^epic.*manifest", re.IGNORECASE),
)
ORIGIN_PATTERNS = (
    re.compile(r"^__installer$", re.IGNORECASE),
    re.compile(r"^origin\.dll$", re.IGNORECASE),
)

ISO_PATTERNS = (
    re.compile(r"\.(iso|img|mdf|mds|nrg|ccd|sub|gdi|chd|wbfs|ciso)$", re.IGNORECASE),
    re.compile(r"\.(cue|toc)$", re.IGNORECASE),
    re.compile(r"\.bin$", re.IGNORECASE),
)
INSTALLER_PATTERNS = (
    re.compile(r"^(setup|install|installer)(\.exe|\.msi)?$", re.IGNORECASE),
    re.compile(r".*_(setup|installer)\.exe$", re.IGNORECASE),
    re.compile(r"^unins\d+\.exe$", re.IGNORECASE),
    re.compile(r"\.msi$", re.IGNORECASE),
    re.compile(r"^autorun\.(exe|inf)$", re.IGNORECASE),
)
EMULATOR_PATTERNS = (
    re.compile(r"^dosbox", re.IGNORECASE),
    re.compile(r"^scummvm", re.IGNORECASE),
    re.compile(r"\.(srm|sav|state|n64|z64|v64|sfc|smc|gb|gbc|gba|nds|3ds|cia|"
               r"nes|fds|sms|gg|md|gen|cue\.?bin)$", re.IGNORECASE),
    re.compile(r"^(roms?|saves?|bios)$", re.IGNORECASE),
    re.compile(r"^(dolphin|pcsx2|ppsspp|rpcs3|citra|yuzu|ryujinx|retroarch|mame|"
               r"snes9x|fceux|project64|epsxe|duckstation|melonDS|desmume)",
               re.IGNORECASE),
    re.compile(r"^dosbox.*\.conf$", re.IGNORECASE),
)
PATCH_PATTERNS = (
    re.compile(r"^patch", re.IGNORECASE),
    re.compile(r".*[_\- ]?patch(ed)?[_\- ]?\d", re.IGNORECASE),
    re.compile(r"^hotfix", re.IGNORECASE),
    re.compile(r"^update(_|\d)", re.IGNORECASE),
)
MANUAL_PATTERNS = (
    re.compile(r"\.(pdf|docx?|rtf|txt|chm)$", re.IGNORECASE),
    re.compile(r"^manual", re.IGNORECASE),
    re.compile(r"^readme", re.IGNORECASE),
)
LAUNCHER_PATTERNS = (
    re.compile(r"^(launcher|start|play|run|bootstrap)\.exe$", re.IGNORECASE),
    re.compile(r".*launcher.*\.exe$", re.IGNORECASE),
)
PORTABLE_PATTERNS = (
    re.compile(r"portable", re.IGNORECASE),
    re.compile(r"^app\w*\.ini$", re.IGNORECASE),      # PortableApps layout
    re.compile(r"^(data|other|source)$", re.IGNORECASE),
)
BACKUP_PATTERNS = (
    re.compile(r"\.(zip|rar|7z|tar|gz|bz2|xz)$", re.IGNORECASE),
    re.compile(r"^(backup|archiwum|kopia)", re.IGNORECASE),
    re.compile(r"\.part\d+\.rar$", re.IGNORECASE),
)
EXECUTABLE_PATTERN = re.compile(r"\.(exe|bat|cmd|com)$", re.IGNORECASE)

# Suggestion order matters: the Manager shows these as pre-ticked chips.
RULES: Sequence[Tuple[str, Tuple[re.Pattern, ...], int]] = (
    ("GOG", GOG_PATTERNS, 1),
    ("Steam", STEAM_PATTERNS, 1),
    ("Epic", EPIC_PATTERNS, 1),
    ("Origin", ORIGIN_PATTERNS, 1),
    ("Emulator", EMULATOR_PATTERNS, 1),
    ("ISO", ISO_PATTERNS, 1),
    ("Installer", INSTALLER_PATTERNS, 1),
    ("Patch", PATCH_PATTERNS, 1),
    ("Portable", PORTABLE_PATTERNS, 2),   # weaker: needs two hits
    ("Backup", BACKUP_PATTERNS, 2),
    ("Launcher", LAUNCHER_PATTERNS, 1),
    ("Manual", MANUAL_PATTERNS, 3),       # weakest: a readme proves very little
)

# Never suggest these from names alone - too noisy to be useful.
NOISY_SUGGESTIONS = frozenset(("EXE",))


@dataclass
class TypeSuggestion(object):
    """One suggested content type and why."""

    name: str
    hits: int
    examples: List[str] = field(default_factory=list)

    def reason(self) -> str:
        if not self.examples:
            return self.name
        shown = ", ".join(self.examples[:2])
        if len(self.examples) > 2:
            shown += ", ..."
        return "%s (%s)" % (self.name, shown)


def suggest_content_types(
    names: Iterable[str],
    folder_name: str = "",
    include_noisy: bool = False,
) -> List[TypeSuggestion]:
    """Suggest content types from a shallow name listing.

    ``names`` should be the file and directory names inside one game folder (not
    full paths). Returns suggestions ordered by confidence.
    """
    listed = [str(name) for name in (names or ()) if name]
    haystack = list(listed)
    if folder_name:
        haystack.append(str(folder_name))

    found: Dict[str, TypeSuggestion] = {}
    for label, patterns, required_hits in RULES:
        if not include_noisy and label in NOISY_SUGGESTIONS:
            continue
        hits = 0
        examples: List[str] = []
        for name in listed:
            for pattern in patterns:
                if pattern.search(name):
                    hits += 1
                    if len(examples) < 3 and name not in examples:
                        examples.append(name)
                    break
        # The folder's own name counts double: a user who called the directory
        # "Some Game Portable" is telling us something deliberate, whereas one
        # stray file named "portable" inside a folder means much less.
        if folder_name:
            for pattern in patterns:
                if pattern.search(str(folder_name)):
                    hits += 2
                    if len(examples) < 3:
                        examples.append(str(folder_name))
                    break
        if hits >= required_hits:
            found[label] = TypeSuggestion(name=label, hits=hits, examples=examples)

    # A folder with a plain .exe and nothing more specific is simply "EXE" - the
    # most common case for a repacked or portable game. It is offered last and
    # only when no stronger label matched.
    if include_noisy or not found:
        if any(EXECUTABLE_PATTERN.search(name) for name in haystack):
            found.setdefault("EXE", TypeSuggestion(name="EXE", hits=1, examples=[]))

    order = [label for label, _patterns, _hits in RULES]
    return sorted(
        found.values(),
        key=lambda suggestion: (
            order.index(suggestion.name) if suggestion.name in order else 99,
            -suggestion.hits,
        ),
    )


def has_executable(names: Iterable[str]) -> bool:
    """Whether the shallow listing contains something runnable (advisory only)."""
    return any(EXECUTABLE_PATTERN.search(str(name)) for name in (names or ()))


def names_from_listing(entries: Iterable[Any], limit: int = DEFAULT_MAX_ENTRIES) -> List[str]:
    """Pull names out of an ``os.scandir`` iterator, bounded.

    Accepts anything iterable that yields objects with a ``.name`` attribute (or
    plain strings), so it is testable without touching the filesystem.
    """
    out: List[str] = []
    for entry in entries:
        if len(out) >= limit:
            break
        name = getattr(entry, "name", None)
        if name is None and isinstance(entry, str):
            name = entry
        if name:
            out.append(str(name))
    return out
