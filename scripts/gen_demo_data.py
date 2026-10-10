#!/usr/bin/env python
"""Generate a clearly synthetic collection for GUI and performance testing.

Why this exists: the brief demands measured performance on a large-ish local
collection ("at least 1,000 lightweight database records should remain practical
to browse") and honest screenshots of the *running* app. Neither is possible
without data, and using a real collection would put someone's library into a
repository. So this script fabricates one and labels it unmistakably.

Everything it produces is fake and self-describing:

* titles are generated from word lists and marked ``SYNTHETIC``;
* artwork is a locally drawn PNG (no network, no provider CDN, no real covers);
* folder paths point into a scratch directory the script creates;
* the database file is named ``demo-collection.db`` and the README it writes says
  what the data is.

It never touches a real games folder, never calls an API and never writes outside
the directory you give it.

    python scripts/gen_demo_data.py --root build/demo --games 1200 --with-artwork
    python scripts/gen_demo_data.py --root build/demo --games 100   # fast smoke data
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from cartridge.artwork import images as image_tools                 # noqa: E402
from cartridge.core.clock import now_iso                            # noqa: E402
from cartridge.core.models import (                                 # noqa: E402
    STATE_COMPLETE,
    STATE_MANUAL,
    STATE_NONE,
    Asset,
)
from cartridge.db.connection import Database                        # noqa: E402
from cartridge.db.repository import Repository                      # noqa: E402
from cartridge.paths import FolderState                             # noqa: E402
from cartridge.text import decade_label                             # noqa: E402

ADJECTIVES = (
    "Crimson Silent Broken Eternal Hollow Iron Lunar Velvet Shattered Frozen "
    "Neon Ancient Rusty Golden Forgotten Hidden Electric Sacred Wandering "
    "Burning Quiet"
).split()
NOUNS = (
    "Empire Signal Garden Fortress Horizon Circuit Harvest Voyage Requiem "
    "Protocol Cathedral Anchor Drift Bastion Echo Machine Crown Tide Archive "
    "Paradox Sentinel Odyssey"
).split()
SUFFIXES = ("", "", "", " II", " III", ": Remastered", " Deluxe", " GOTY", " 2077")
GENRES = (
    "Role-playing (RPG) Action Adventure Strategy Simulation Racing Puzzle "
    "Shooter Platformer Fighting Sports Horror Tactical Roguelike"
).split()
DEVELOPERS = [
    "Northlight Studio", "Ironvale", "Pixel Foundry", "Blackmoor Games",
    "Cascade Works", "Halcyon Interactive", "Vostok Labs", "Meridian Softworks",
]
PUBLISHERS = [
    "Northlight Publishing", "Atlas Interactive", "Vostok Media", "Indie Self-Released",
]
PLATFORMS = ["PC (Microsoft Windows)", "Linux", "Mac", "DOS"]
CONTENT_POOL = ["GOG", "EXE", "ISO", "Installer", "Portable", "Emulator", "Manual"]


def make_title(rng: random.Random, index: int) -> str:
    return "SYNTHETIC %s %s%s #%04d" % (
        rng.choice(ADJECTIVES),
        rng.choice(NOUNS),
        rng.choice(SUFFIXES),
        index,
    )


def make_cover_png(path: str, rng: random.Random, index: int, width=180, height=252) -> bool:
    """Draw a distinct flat-colour placeholder cover. No downloaded artwork."""
    from tests._png import png_bytes

    color = (
        30 + rng.randrange(90),
        40 + rng.randrange(90),
        70 + rng.randrange(120),
    )
    data = png_bytes(width, height, color)
    try:
        with open(path, "wb") as handle:
            handle.write(data)
    except OSError:
        return False
    return True


def build(args) -> int:
    rng = random.Random(args.seed)
    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        os.makedirs(root)

    db_path = os.path.join(root, ".cartridge", "demo-collection.db")
    if not os.path.isdir(os.path.dirname(db_path)):
        os.makedirs(os.path.dirname(db_path))
    if os.path.exists(db_path) and not args.keep:
        os.remove(db_path)

    started = time.time()
    database = Database(db_path).open()
    repo = Repository(database, windows=False)
    repo.add_root(root, label="Demo root (synthetic data)")

    artwork_count = 0
    for index in range(1, args.games + 1):
        folder_name = "%04d %s" % (index, make_title(rng, index).replace("SYNTHETIC ", "").replace(" ", "_")[:34])
        folder = os.path.join(root, folder_name)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        # A couple of files so folder scans and size measurement have something
        # to look at; nothing executable is created beyond an empty stub name.
        exe = os.path.join(folder, "game.exe")
        if not os.path.exists(exe):
            with open(exe, "wb") as handle:
                handle.write(b"MZ")
                handle.write(os.urandom(rng.randrange(64, 4096)))

        state = FolderState.PRESENT.value
        if args.simulate_missing and index % args.simulate_missing == 0:
            state = FolderState.MISSING.value

        game, _created = repo.upsert_folder(folder, state=state)
        if game is None:
            continue

        year = rng.randrange(1993, 2026)
        metadata_state = STATE_COMPLETE
        roll = rng.random()
        if roll < args.incomplete_ratio:
            metadata_state = STATE_NONE
        elif roll < args.incomplete_ratio + 0.05:
            metadata_state = STATE_MANUAL

        payload = {
            "title": make_title(rng, index),
            "summary": (
                "A synthetic record generated by scripts/gen_demo_data.py for "
                "performance and UI testing. It does not describe a real game."
            ),
            "release_date": "%04d-%02d-%02d" % (year, rng.randrange(1, 13), rng.randrange(1, 28)),
            "release_year": year,
            "rating": round(rng.uniform(35.0, 97.0), 2) if rng.random() > 0.18 else None,
            "developer": rng.choice(DEVELOPERS),
            "publisher": rng.choice(PUBLISHERS),
            "franchise": rng.choice(NOUNS) + " series" if rng.random() > 0.6 else None,
            "genres": rng.sample(GENRES, rng.randrange(1, 4)),
            "platforms": rng.sample(PLATFORMS, rng.randrange(1, 3)),
            "alternative_names": [
                make_title(rng, index).replace("SYNTHETIC ", "").lower()
            ] if rng.random() > 0.7 else [],
            "age_rating": rng.choice(["PEGI 16", "PEGI 18", "ESRB: T", None]),
            "provider": "manual" if metadata_state == STATE_MANUAL else None,
            "provider_id": None,
            "metadata_state": metadata_state,
        }
        if metadata_state == STATE_NONE:
            payload = {"title": payload["title"], "metadata_state": STATE_NONE}
            repo.apply_provider_metadata(game.id, payload, mark_complete=False)
        else:
            repo.apply_provider_metadata(game.id, payload)

        repo.set_game_content_types(
            game.id, rng.sample(CONTENT_POOL, rng.randrange(1, 3)), source="suggested"
        )
        if rng.random() < args.favourite_ratio:
            repo.set_favourite(game.id, True)
        if rng.random() < 0.1:
            repo.update_user_fields(game.id, {"user_title": payload["title"] + " (mine)"})
        if rng.random() < 0.15:
            repo.set_notes(game.id, "Synthetic note for testing the notes field.")

        if args.with_artwork and rng.random() > 0.12:
            asset_dir = os.path.join(folder, "_cartridge", "assets")
            if not os.path.isdir(asset_dir):
                os.makedirs(asset_dir)
            cover_path = os.path.join(asset_dir, "cover.png")
            if make_cover_png(cover_path, rng, index):
                validation = image_tools.validate_file(cover_path)
                repo.add_asset(
                    game.id,
                    Asset(
                        kind="cover",
                        rel_path="_cartridge/assets/cover.png",
                        provider="demo",
                        width=validation.get("width"),
                        height=validation.get("height"),
                        size_bytes=validation.get("bytes"),
                        sha256=image_tools.sha256_file(cover_path),
                        state="ok",
                        downloaded_at=now_iso(),
                    ),
                )
                artwork_count += 1
                if rng.random() < 0.5:
                    thumb_dir = os.path.join(asset_dir, "thumbs")
                    if not os.path.isdir(thumb_dir):
                        os.makedirs(thumb_dir)
                    image_tools.make_thumbnail(
                        cover_path, os.path.join(thumb_dir, "cover.jpg"),
                        image_tools.COVER_THUMB,
                    )
            for shot in range(rng.randrange(0, args.screenshots + 1)):
                name = "screenshot-%02d.png" % (shot + 1)
                shot_path = os.path.join(asset_dir, name)
                if make_cover_png(shot_path, rng, index, width=320, height=180):
                    repo.add_asset(
                        game.id,
                        Asset(
                            kind="screenshot",
                            rel_path="_cartridge/assets/%s" % name,
                            provider="demo",
                            sort_order=shot + 1,
                            state="ok",
                            downloaded_at=now_iso(),
                        ),
                    )

        if args.with_sizes and rng.random() < 0.35:
            from cartridge.scan.folder_size import measure_folder_size

            measured = measure_folder_size(folder)
            repo.set_folder_size(game.id, measured.size_bytes, measured.measured_at)

        if index % 250 == 0:
            print("  %d/%d games..." % (index, args.games), flush=True)

    database.vacuum()
    stats = repo.stats()
    elapsed = time.time() - started
    database.close_all()

    readme = os.path.join(root, "SYNTHETIC-DATA.txt")
    with open(readme, "w", encoding="utf-8") as handle:
        handle.write(
            "This directory contains SYNTHETIC data generated by\n"
            "Cartridge's scripts/gen_demo_data.py for performance and UI testing.\n\n"
            "Nothing here describes a real game. Every title is prefixed SYNTHETIC,\n"
            "every cover is a locally drawn flat-colour PNG, and no network request\n"
            "was made to produce it.\n\n"
            "Safe to delete.\n\n"
            "Generated: %s\nGames: %d\nArtwork files: %d\nBuild time: %.1f s\n"
            % (now_iso(), args.games, artwork_count, elapsed)
        )

    print("")
    print("Synthetic collection written to %s" % root)
    print("  database      : %s" % db_path)
    print("  games         : %d" % stats["games"])
    print("  with cover    : %d" % stats["with_cover"])
    print("  favourites    : %d" % stats["favourites"])
    print("  incomplete    : %d" % stats["incomplete"])
    print("  content types : %d" % stats["content_types"])
    print("  artwork files : %d" % artwork_count)
    print("  build time    : %.1f s" % elapsed)
    print("")
    print("Open it with:")
    print("  python run.py --db \"%s\"" % db_path)
    print("")
    print("All data is synthetic and clearly labelled. Delete the folder to remove it.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="scratch directory to build inside")
    parser.add_argument("--games", type=int, default=1000)
    parser.add_argument("--with-artwork", action="store_true", help="draw placeholder covers")
    parser.add_argument("--with-sizes", action="store_true", help="measure folder sizes")
    parser.add_argument("--screenshots", type=int, default=2)
    parser.add_argument("--favourite-ratio", type=float, default=0.08)
    parser.add_argument("--incomplete-ratio", type=float, default=0.12)
    parser.add_argument(
        "--simulate-missing", type=int, default=0,
        help="mark every Nth game's folder as missing (0 disables)",
    )
    parser.add_argument("--seed", type=int, default=20260109)
    parser.add_argument("--keep", action="store_true", help="reuse an existing database")
    args = parser.parse_args(argv)

    if args.games < 1 or args.games > 200000:
        parser.error("--games must be between 1 and 200000")
    return build(args)


if __name__ == "__main__":
    sys.exit(main())
