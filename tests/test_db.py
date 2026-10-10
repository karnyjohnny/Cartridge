"""Tests for the SQLite layer: schema, migrations, repository, transactions.

Every test uses a real temporary database file — the point is to exercise the
actual SQL, constraints and transaction behaviour, not a mock.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from cartridge.core import filtering
from cartridge.core.clock import now_iso
from cartridge.core.models import (
    STATE_COMPLETE,
    STATE_MANUAL,
    STATE_NONE,
    STATE_PARTIAL,
    Asset,
)
from cartridge.db.connection import (
    Database,
    DatabaseError,
    SchemaTooNewError,
    is_writable_location,
    suggest_fallback_db_path,
)
from cartridge.db.repository import Repository, SecretInDatabaseError
from cartridge.paths import FolderState


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "collection.db")).open()
    yield database
    database.close_all()


@pytest.fixture
def repo(db):
    return Repository(db, windows=False)


# --------------------------------------------------------------------------
# schema + migrations
# --------------------------------------------------------------------------
def test_new_database_is_created_at_latest_version(db):
    assert db.created_new is True
    assert db.schema_version >= 1
    assert db.integrity_check()[0] is True


def test_expected_tables_exist(db):
    rows = db.query("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
    names = {row["name"] for row in rows}
    for expected in (
        "games", "roots", "assets", "content_types", "game_content_types",
        "genres", "game_genres", "platforms", "game_platforms", "game_aliases",
        "settings", "scan_runs", "migrations", "meta", "op_samples",
        "game_user_genres",
    ):
        assert expected in names, "missing table %s" % expected


def test_foreign_keys_are_enforced(db):
    assert int(db.scalar("PRAGMA foreign_keys", default=0)) == 1
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO assets (game_id, kind, rel_path) VALUES (9999, 'cover', 'x.jpg')"
        )


def test_migration_history_is_recorded(db):
    history = db.migration_history()
    assert history, "no migration rows recorded"
    assert history[0]["version"] == 1
    assert history[0]["note"]


def test_reopen_is_idempotent(tmp_path):
    path = str(tmp_path / "collection.db")
    first = Database(path).open()
    Repository(first).add_root(str(tmp_path))
    first_version = first.schema_version
    first_history = first.migration_history()
    first.close_all()

    second = Database(path).open()
    try:
        assert second.created_new is False
        assert second.schema_version == first_version
        assert len(Repository(second).list_roots()) == 1
        # reopening must not re-apply or duplicate migrations
        assert second.migration_history() == first_history
        assert len(second.migration_history()) == 1
    finally:
        second.close_all()


def test_schema_newer_than_build_is_refused(tmp_path):
    """Opening a database from a future build must fail loudly, not corrupt it."""
    path = str(tmp_path / "future.db")
    seed = sqlite3.connect(path)
    seed.execute("CREATE TABLE migrations (version INTEGER PRIMARY KEY, applied_at TEXT, note TEXT)")
    seed.execute("INSERT INTO migrations VALUES (999, '2030-01-01T00:00:00Z', 'future')")
    seed.execute("PRAGMA user_version = 999")
    seed.commit()
    seed.close()

    with pytest.raises(SchemaTooNewError):
        Database(path).open()


def test_version_inferred_from_migrations_table_when_user_version_is_zero(tmp_path):
    path = str(tmp_path / "handmade.db")
    seed = sqlite3.connect(path)
    from cartridge.db.schema import SCHEMA_V1

    for statement in SCHEMA_V1:
        seed.execute(statement)
    seed.execute(
        "INSERT INTO migrations (version, applied_at, note) VALUES (1, ?, 'baseline')",
        (now_iso(),),
    )
    seed.commit()
    seed.close()

    database = Database(path).open()
    try:
        assert database.schema_version == 1
        assert any("adopted it" in note for note in database.pragma_notes)
    finally:
        database.close_all()


def test_journal_mode_is_recorded(db):
    assert db.journal_mode in ("wal", "delete", "memory")


def test_default_content_types_are_seeded(repo):
    names = [ct.name for ct in repo.list_content_types(with_counts=False)]
    for expected in ("GOG", "EXE", "ISO", "Installer", "Emulator"):
        assert expected in names


# --------------------------------------------------------------------------
# roots
# --------------------------------------------------------------------------
def test_add_root_is_idempotent_by_key(repo, tmp_path):
    root_path = str(tmp_path / "Gry")
    os.makedirs(root_path, exist_ok=True)
    first = repo.add_root(root_path)
    second = repo.add_root(root_path)
    assert first.id == second.id
    assert len(repo.list_roots()) == 1


def test_remove_root_keeps_games_by_default(repo, tmp_path):
    root = repo.add_root(str(tmp_path))
    game, created = repo.upsert_folder(str(tmp_path / "Game"), root_id=root.id)
    assert created
    repo.remove_root(root.id, delete_games=False)
    assert repo.list_roots() == []
    kept = repo.get_game(game.id)
    assert kept is not None
    assert kept.root_id is None


def test_remove_root_can_delete_games(repo, tmp_path):
    root = repo.add_root(str(tmp_path))
    game, _ = repo.upsert_folder(str(tmp_path / "Game"), root_id=root.id)
    repo.remove_root(root.id, delete_games=True)
    assert repo.get_game(game.id) is None


# --------------------------------------------------------------------------
# folder identity / duplicates
# --------------------------------------------------------------------------
def test_same_folder_cannot_create_two_records(repo, tmp_path):
    folder = str(tmp_path / "Wiedzmin 2")
    os.makedirs(folder)
    game_a, created_a = repo.upsert_folder(folder)
    game_b, created_b = repo.upsert_folder(folder)
    assert created_a is True
    assert created_b is False
    assert game_a.id == game_b.id
    assert repo.db.scalar("SELECT COUNT(*) FROM games", default=0) == 1


def test_windows_case_and_separator_variants_are_one_folder(tmp_path):
    """The core duplicate-prevention rule on Windows."""
    database = Database(str(tmp_path / "win.db")).open()
    try:
        windows_repo = Repository(database, windows=True)
        variants = [
            r"E:\Gry\Wiedzmin 2",
            r"e:\gry\wiedzmin 2",
            "E:/Gry/Wiedzmin 2",
            r"E:\Gry\Wiedzmin 2\\",
        ]
        ids = set()
        for variant in variants:
            game, _created = windows_repo.upsert_folder(variant)
            ids.add(game.id)
        assert len(ids) == 1
        assert windows_repo.db.scalar("SELECT COUNT(*) FROM games", default=0) == 1
    finally:
        database.close_all()


def test_posix_paths_stay_distinct_by_case(repo, tmp_path):
    lower = str(tmp_path / "game")
    upper = str(tmp_path / "GAME")
    os.makedirs(lower)
    os.makedirs(upper)
    a, _ = repo.upsert_folder(lower)
    b, _ = repo.upsert_folder(upper)
    assert a.id != b.id


def test_folder_state_is_tracked_not_deleted(repo, tmp_path):
    folder = str(tmp_path / "Gone")
    os.makedirs(folder)
    game, _ = repo.upsert_folder(folder)
    os.rmdir(folder)
    repo.set_folder_state(game.id, FolderState.MISSING.value)
    still = repo.get_game(game.id)
    assert still is not None, "metadata must survive a vanished folder"
    assert still.folder_state == FolderState.MISSING.value
    assert still.title


def test_reassociate_folder_moves_identity(repo, tmp_path):
    old = tmp_path / "Old Name"
    new = tmp_path / "New Name"
    old.mkdir()
    new.mkdir()
    game, _ = repo.upsert_folder(str(old))
    assert repo.reassociate_folder(game.id, str(new)) is True
    moved = repo.get_game(game.id)
    assert moved.folder_path == str(new)
    assert moved.folder_state == FolderState.PRESENT.value
    # the old key must be free again
    assert repo.get_game_by_folder(str(old)) is None


def test_reassociate_refuses_to_collide(repo, tmp_path):
    a_dir = tmp_path / "A"
    b_dir = tmp_path / "B"
    a_dir.mkdir()
    b_dir.mkdir()
    a, _ = repo.upsert_folder(str(a_dir))
    b, _ = repo.upsert_folder(str(b_dir))
    with pytest.raises(DatabaseError):
        repo.reassociate_folder(a.id, str(b_dir))
    assert repo.get_game(b.id).folder_path == str(b_dir)


# --------------------------------------------------------------------------
# provider refresh must never destroy user edits
# --------------------------------------------------------------------------
def test_provider_refresh_preserves_user_overrides(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.apply_provider_metadata(
        game.id,
        {
            "title": "Provider Title",
            "summary": "Provider summary",
            "release_year": 2001,
            "rating": 80.0,
            "developer": "Provider Dev",
            "genres": ["RPG"],
            "provider": "igdb",
            "provider_id": "1234",
        },
    )
    repo.update_user_fields(
        game.id,
        {
            "user_title": "My Title",
            "user_summary": "My notes about this game",
            "user_release_year": 1999,
            "user_rating": 95.0,
        },
    )
    before = repo.get_game(game.id)
    assert before.effective_title() == "My Title"
    assert before.effective_year() == 1999

    # A second refresh with different provider values
    repo.apply_provider_metadata(
        game.id,
        {
            "title": "Corrected Provider Title",
            "summary": "Corrected summary",
            "release_year": 2002,
            "rating": 70.0,
            "provider": "rawg",
            "provider_id": "999",
        },
    )
    after = repo.get_game(game.id)

    assert after.user_title == "My Title", "user title was overwritten"
    assert after.user_summary == "My notes about this game"
    assert after.user_release_year == 1999
    assert after.user_rating == 95.0
    assert after.effective_title() == "My Title"
    assert after.effective_year() == 1999
    # provider columns did update
    assert after.title == "Corrected Provider Title"
    assert after.provider == "rawg"


def test_clearing_an_override_falls_back_to_provider(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.apply_provider_metadata(game.id, {"title": "Provider Title"})
    repo.update_user_fields(game.id, {"user_title": "Mine"})
    assert repo.get_game(game.id).effective_title() == "Mine"
    repo.update_user_fields(game.id, {"user_title": None})
    assert repo.get_game(game.id).effective_title() == "Provider Title"


def test_update_user_fields_rejects_unknown_columns(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    with pytest.raises(ValueError):
        repo.update_user_fields(game.id, {"folder_path": "/etc/passwd"})
    with pytest.raises(ValueError):
        repo.update_user_fields(game.id, {"provider": "hack"})


def test_user_genres_override_provider_genres(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.apply_provider_metadata(game.id, {"title": "T", "genres": ["RPG", "Action"]})
    repo.update_user_fields(game.id, {"user_genres": ["Tactics"]})
    after = repo.get_game(game.id)
    assert after.genres == ["Action", "RPG"]
    assert after.effective_genres() == ["Tactics"]


def test_manual_metadata_marks_state(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.set_manual_metadata(
        game.id, {"title": "Typed By Hand", "release_year": 1994, "genres": ["FPS"]}
    )
    after = repo.get_game(game.id)
    assert after.metadata_state == STATE_MANUAL
    assert after.provider is None
    assert after.effective_title() == "Typed By Hand"
    assert after.release_year == 1994


def test_metadata_error_is_recorded_not_hidden(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.set_metadata_error(game.id, "HTTP 429 rate limited")
    after = repo.get_game(game.id)
    assert after.metadata_state == "error"
    assert "429" in after.metadata_error
    assert after.needs_attention() is True


def test_provider_identity_is_unique(repo, tmp_path):
    a, _ = repo.upsert_folder(str(tmp_path / "A"))
    b, _ = repo.upsert_folder(str(tmp_path / "B"))
    repo.apply_provider_metadata(a.id, {"title": "X", "provider": "igdb", "provider_id": "5"})
    with pytest.raises(sqlite3.IntegrityError):
        repo.apply_provider_metadata(
            b.id, {"title": "X", "provider": "igdb", "provider_id": "5"}
        )


def test_manual_entries_may_share_null_provider_ids(repo, tmp_path):
    a, _ = repo.upsert_folder(str(tmp_path / "A"))
    b, _ = repo.upsert_folder(str(tmp_path / "B"))
    repo.set_manual_metadata(a.id, {"title": "A"})
    repo.set_manual_metadata(b.id, {"title": "B"})
    assert repo.get_game(a.id).id != repo.get_game(b.id).id


# --------------------------------------------------------------------------
# content types
# --------------------------------------------------------------------------
def test_content_types_can_be_added_renamed_removed(repo, tmp_path):
    ct = repo.add_content_type("Modded")
    assert ct.id is not None
    repo.rename_content_type(ct.id, "Modded (merged)")
    names = [c.name for c in repo.list_content_types(with_counts=False)]
    assert "Modded (merged)" in names
    assert "Modded" not in names
    repo.delete_content_type(ct.id)
    names = [c.name for c in repo.list_content_types(with_counts=False)]
    assert "Modded (merged)" not in names


def test_content_type_dedupes_on_case_and_whitespace(repo):
    first = repo.add_content_type("GOG")
    second = repo.add_content_type("  gog ")
    assert first.id == second.id


def test_rename_to_existing_name_is_refused(repo):
    repo.add_content_type("Alpha")
    beta = repo.add_content_type("Beta")
    with pytest.raises(ValueError):
        repo.rename_content_type(beta.id, "alpha")


def test_empty_content_type_name_is_refused(repo):
    with pytest.raises(ValueError):
        repo.add_content_type("   ")


def test_game_content_types_roundtrip(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.set_game_content_types(game.id, ["GOG", "Installer"], source="user")
    after = repo.get_game(game.id)
    assert sorted(after.content_types) == ["GOG", "Installer"]

    repo.set_game_content_types(game.id, ["ISO"], source="user")
    after = repo.get_game(game.id)
    assert after.content_types == ["ISO"], "user types must be replaced, not merged"


def test_suggested_types_do_not_clobber_user_types(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.set_game_content_types(game.id, ["GOG"], source="user")
    repo.set_game_content_types(game.id, ["EXE", "Installer"], source="suggested")
    after = repo.get_game(game.id)
    assert "GOG" in after.content_types
    assert "EXE" in after.content_types


def test_deleting_a_content_type_keeps_the_game(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.set_game_content_types(game.id, ["Temp"])
    ct = [c for c in repo.list_content_types(with_counts=False) if c.name == "Temp"][0]
    repo.delete_content_type(ct.id)
    assert repo.get_game(game.id) is not None
    assert repo.get_game(game.id).content_types == []


# --------------------------------------------------------------------------
# assets
# --------------------------------------------------------------------------
def test_asset_roundtrip(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.add_asset(
        game.id,
        Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg", width=300,
              height=400, size_bytes=1234, sha256="a" * 64, provider="igdb",
              source_url="https://images.igdb.com/x.jpg"),
    )
    repo.add_asset(
        game.id,
        Asset(kind="screenshot", rel_path="_cartridge/assets/screenshot-01.jpg",
              sort_order=1),
    )
    after = repo.get_game(game.id)
    assert after.cover() is not None
    assert after.cover().rel_path.endswith("cover.jpg")
    assert len(after.screenshots()) == 1
    assert after.artwork_state() == "ok"


def test_artwork_state_reports_missing_and_partial(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    assert repo.get_game(game.id).artwork_state() == "missing"
    repo.add_asset(game.id, Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg"))
    assert repo.get_game(game.id).artwork_state() == "partial"


def test_mark_missing_assets_does_not_delete_records(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.add_asset(game.id, Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg"))
    repo.add_asset(game.id, Asset(kind="screenshot", rel_path="_cartridge/assets/s1.jpg"))
    changed = repo.mark_missing_assets(game.id, ["_cartridge/assets/cover.jpg"])
    assert changed == 1
    assets = repo.list_assets(game.id)
    assert len(assets) == 2, "records must be kept, only the state changes"
    states = {a.rel_path: a.state for a in assets}
    assert states["_cartridge/assets/cover.jpg"] == "ok"
    assert states["_cartridge/assets/s1.jpg"] == "missing"
    # a missing cover is not usable
    after = repo.get_game(game.id)
    assert after.cover() is not None
    repo.mark_missing_assets(game.id, [])
    assert repo.get_game(game.id).cover() is None


def test_duplicate_asset_is_replaced_not_duplicated(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.add_asset(game.id, Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg", sha256="a"))
    repo.add_asset(game.id, Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg", sha256="b"))
    assets = repo.list_assets(game.id, kind="cover")
    assert len(assets) == 1
    assert assets[0].sha256 == "b"


def test_deleting_a_game_cascades_to_assets(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.add_asset(game.id, Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg"))
    repo.set_game_content_types(game.id, ["GOG"])
    repo.delete_game(game.id)
    assert repo.db.scalar("SELECT COUNT(*) FROM assets", default=0) == 0
    assert repo.db.scalar("SELECT COUNT(*) FROM game_content_types", default=0) == 0


# --------------------------------------------------------------------------
# settings + secret refusal
# --------------------------------------------------------------------------
def test_settings_roundtrip(repo):
    repo.set_setting("theme", "dark")
    assert repo.get_setting("theme") == "dark"
    repo.set_setting("theme", "light")
    assert repo.get_setting("theme") == "light"
    assert repo.get_setting("missing", "fallback") == "fallback"
    repo.set_setting("grid_columns", 5)
    assert repo.get_int("grid_columns", 3) == 5
    repo.set_setting("show_missing", "true")
    assert repo.get_bool("show_missing", False) is True
    assert repo.all_settings()["theme"] == "light"


@pytest.mark.parametrize(
    "key", ["igdb_client_secret", "rawg_api_key", "client_secret", "token", "password"]
)
def test_secrets_are_refused_by_the_database(repo, key):
    with pytest.raises(SecretInDatabaseError):
        repo.set_setting(key, "some-value")
    assert repo.get_setting(key) is None


def test_no_secret_value_is_stored_anywhere(db, repo):
    """Belt and braces: scan every table for a value we tried to persist."""
    marker = "SUPERSECRETMARKERVALUE"
    with pytest.raises(SecretInDatabaseError):
        repo.set_setting("igdb_client_secret", marker)
    repo.set_setting("harmless", marker)
    dumped = "\n".join(db.conn.iterdump())
    # the harmless setting is allowed; a credentials row must not exist
    assert "igdb_client_secret" not in dumped


# --------------------------------------------------------------------------
# filtering + sorting
# --------------------------------------------------------------------------
def _seed_games(repo, tmp_path):
    specs = [
        ("Alpha Game", 1998, 80.0, True, ["RPG"], ["GOG"], "Dev One"),
        ("Beta Quest", 2005, 90.0, False, ["Action"], ["ISO"], "Dev Two"),
        ("Gamma Tales", None, None, False, ["RPG"], [], "Dev One"),
        ("the Delta Story", 2011, 60.0, True, ["Adventure"], ["Installer"], "Dev Three"),
    ]
    ids = []
    for index, (title, year, rating, fav, genres, types, dev) in enumerate(specs):
        folder = tmp_path / ("g%02d" % index)
        folder.mkdir()
        game, _ = repo.upsert_folder(str(folder))
        repo.apply_provider_metadata(
            game.id,
            {
                "title": title, "release_year": year, "rating": rating,
                "genres": genres, "developer": dev, "provider": "igdb",
                "provider_id": str(1000 + index),
            },
        )
        repo.set_favourite(game.id, fav)
        if types:
            repo.set_game_content_types(game.id, types)
        ids.append(game.id)
    return ids


def test_search_matches_title_and_is_case_insensitive(repo, tmp_path):
    _seed_games(repo, tmp_path)
    spec = filtering.FilterSpec(query="alpha")
    results = repo.list_games(spec)
    assert [g.effective_title() for g in results] == ["Alpha Game"]


def test_search_matches_multi_word_query_in_any_order(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "w3"))
    repo.apply_provider_metadata(
        game.id, {"title": "The Witcher 3: Wild Hunt", "provider": "igdb", "provider_id": "7"}
    )
    for query in ("witcher wild", "wild hunt witcher", "witcher3", "WILD HUNT"):
        assert repo.count_games(filtering.FilterSpec(query=query)) == 1, query
    assert repo.count_games(filtering.FilterSpec(query="diablo")) == 0


def test_search_covers_people_genres_and_content_types(repo, tmp_path):
    _seed_games(repo, tmp_path)
    assert repo.count_games(filtering.FilterSpec(query="Dev Two")) == 1
    assert repo.count_games(filtering.FilterSpec(query="Adventure")) == 1
    assert repo.count_games(filtering.FilterSpec(query="Installer")) == 1


def test_search_matches_alternative_names(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "w2"))
    repo.apply_provider_metadata(
        game.id,
        {
            "title": "The Witcher 2: Enhanced Edition",
            "alternative_names": ["Wiedźmin 2: Zabójcy Królów"],
            "provider": "igdb", "provider_id": "8",
        },
    )
    assert repo.count_games(filtering.FilterSpec(query="wiedzmin")) == 1


def test_view_favourites(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(view=filtering.VIEW_FAVOURITES))
    assert sorted(g.effective_title() for g in results) == ["Alpha Game", "the Delta Story"]


def test_view_missing_artwork(repo, tmp_path):
    ids = _seed_games(repo, tmp_path)
    repo.add_asset(ids[0], Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg"))
    results = repo.list_games(filtering.FilterSpec(view=filtering.VIEW_MISSING_ARTWORK))
    assert len(results) == 3
    assert all(g.id != ids[0] for g in results)


def test_view_missing_folder(repo, tmp_path):
    ids = _seed_games(repo, tmp_path)
    repo.set_folder_state(ids[1], FolderState.MISSING.value)
    repo.set_folder_state(ids[2], FolderState.UNAVAILABLE_DRIVE.value)
    results = repo.list_games(filtering.FilterSpec(view=filtering.VIEW_MISSING_FOLDER))
    assert {g.id for g in results} == {ids[1], ids[2]}


def test_view_needs_attention_combines_reasons(repo, tmp_path):
    ids = _seed_games(repo, tmp_path)
    repo.add_asset(ids[0], Asset(kind="cover", rel_path="_cartridge/assets/cover.jpg"))
    repo.set_folder_state(ids[1], FolderState.MISSING.value)
    results = {g.id for g in repo.list_games(
        filtering.FilterSpec(view=filtering.VIEW_NEEDS_ATTENTION)
    )}
    # ids[0] now has a cover, is present on disk and has complete metadata, so
    # it must drop out. ids[1] stays because its folder is missing; the rest
    # have no artwork at all.
    assert ids[0] not in results
    assert set(ids[1:]) <= results


def test_genre_facet_filter(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(genres=["RPG"]))
    assert sorted(g.effective_title() for g in results) == ["Alpha Game", "Gamma Tales"]


def test_content_type_filter(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(content_types=["ISO"]))
    assert [g.effective_title() for g in results] == ["Beta Quest"]


def test_decade_filter(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(decades=["1990s"]))
    assert [g.effective_title() for g in results] == ["Alpha Game"]
    results = repo.list_games(filtering.FilterSpec(decades=["2000s", "2010s"]))
    assert sorted(g.effective_title() for g in results) == ["Beta Quest", "the Delta Story"]


def test_decade_range_parsing():
    assert filtering.decade_range("1990s") == (1990, 1999)
    assert filtering.decade_range("2010") == (2010, 2019)
    assert filtering.decade_range("nonsense") is None
    assert filtering.decade_range("") is None


def test_developer_and_min_rating_filters(repo, tmp_path):
    _seed_games(repo, tmp_path)
    assert repo.count_games(filtering.FilterSpec(developers=["Dev One"])) == 2
    assert repo.count_games(filtering.FilterSpec(min_rating=75.0)) == 2
    assert repo.count_games(filtering.FilterSpec(min_rating=95.0)) == 0


def test_combined_filters_are_anded(repo, tmp_path):
    _seed_games(repo, tmp_path)
    spec = filtering.FilterSpec(genres=["RPG"], view=filtering.VIEW_FAVOURITES)
    results = repo.list_games(spec)
    assert [g.effective_title() for g in results] == ["Alpha Game"]


def test_sort_by_title_ignores_leading_article(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(sort="title"))
    titles = [g.effective_title() for g in results]
    assert titles == ["Alpha Game", "Beta Quest", "the Delta Story", "Gamma Tales"]


def test_sort_by_title_descending(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(sort="title", descending=True))
    assert results[0].effective_title() == "Gamma Tales"


def test_sort_by_year_puts_unknown_last_in_both_directions(repo, tmp_path):
    _seed_games(repo, tmp_path)
    ascending = repo.list_games(filtering.FilterSpec(sort="year"))
    descending = repo.list_games(filtering.FilterSpec(sort="year", descending=True))
    assert ascending[-1].effective_title() == "Gamma Tales"   # year is None
    assert descending[-1].effective_title() == "Gamma Tales"


def test_sort_by_rating_and_added(repo, tmp_path):
    _seed_games(repo, tmp_path)
    by_rating = repo.list_games(
        filtering.FilterSpec(sort="rating", descending=True)
    )
    assert by_rating[0].effective_title() == "Beta Quest"
    assert by_rating[-1].effective_title() == "Gamma Tales"  # NULL rating last
    by_added = repo.list_games(filtering.FilterSpec(sort="added"))
    assert len(by_added) == 4


def test_unknown_sort_falls_back_to_title(repo, tmp_path):
    _seed_games(repo, tmp_path)
    spec = filtering.FilterSpec(sort="DROP TABLE games; --")
    results = repo.list_games(spec)
    assert len(results) == 4
    assert repo.db.scalar("SELECT COUNT(*) FROM games", default=0) == 4


def test_limit_and_offset_page_stably(repo, tmp_path):
    _seed_games(repo, tmp_path)
    first = repo.list_games(filtering.FilterSpec(sort="title", limit=2, offset=0))
    second = repo.list_games(filtering.FilterSpec(sort="title", limit=2, offset=2))
    assert [g.effective_title() for g in first] == ["Alpha Game", "Beta Quest"]
    assert [g.effective_title() for g in second] == ["the Delta Story", "Gamma Tales"]
    assert {g.id for g in first}.isdisjoint({g.id for g in second})


def test_offset_without_limit(repo, tmp_path):
    _seed_games(repo, tmp_path)
    results = repo.list_games(filtering.FilterSpec(sort="title", offset=3))
    assert len(results) == 1


def test_count_matches_list(repo, tmp_path):
    _seed_games(repo, tmp_path)
    spec = filtering.FilterSpec(genres=["RPG"])
    assert repo.count_games(spec) == len(repo.list_games(spec))


def test_facets_report_counts(repo, tmp_path):
    _seed_games(repo, tmp_path)
    facets = repo.facets()
    genres = dict(facets["genres"])
    assert genres.get("RPG") == 2
    assert genres.get("Action") == 1
    decades = dict(facets["decades"])
    assert decades.get("1990s") == 1
    developers = dict(facets["developers"])
    assert developers.get("Dev One") == 2


def test_sql_injection_in_values_is_inert(repo, tmp_path):
    """Values must be bound, never interpolated."""
    hostile = str(tmp_path / "Game') ; DROP TABLE games; --")
    os.makedirs(hostile, exist_ok=True)
    game, _ = repo.upsert_folder(hostile)
    repo.apply_provider_metadata(
        game.id,
        {
            "title": "'; DROP TABLE games; --",
            "summary": "\" OR 1=1 --",
            "developer": "Robert'); DROP TABLE students;--",
        },
    )
    assert repo.db.scalar("SELECT COUNT(*) FROM games", default=-1) == 1
    found = repo.list_games(filtering.FilterSpec(query="DROP"))
    assert len(found) == 1
    assert found[0].title == "'; DROP TABLE games; --"


# --------------------------------------------------------------------------
# transactions / recovery
# --------------------------------------------------------------------------
def test_failed_transaction_rolls_back(db, repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.apply_provider_metadata(game.id, {"title": "Before"})
    before_count = repo.db.scalar("SELECT COUNT(*) FROM game_aliases", default=0)

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO game_aliases (game_id, name, name_key) VALUES (?, ?, ?)",
                (game.id, "Temp", "temp"),
            )
            raise RuntimeError("simulated failure mid-import")

    after_count = repo.db.scalar("SELECT COUNT(*) FROM game_aliases", default=0)
    assert after_count == before_count
    assert repo.get_game(game.id).title == "Before"


def test_incomplete_import_is_visible_as_incomplete(repo, tmp_path):
    """A game whose artwork failed must not look complete."""
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    assert game.metadata_state == STATE_NONE
    repo.apply_provider_metadata(game.id, {"title": "Half Done"}, mark_complete=False)
    after = repo.get_game(game.id)
    assert after.metadata_state == STATE_PARTIAL
    assert after.needs_attention() is True


def test_mark_complete_sets_state(repo, tmp_path):
    game, _ = repo.upsert_folder(str(tmp_path / "Game"))
    repo.apply_provider_metadata(game.id, {"title": "Done"})
    assert repo.get_game(game.id).metadata_state == STATE_COMPLETE


def test_backup_produces_a_readable_copy(db, repo, tmp_path):
    _seed_games(repo, tmp_path)
    destination = str(tmp_path / "backup" / "copy.db")
    db.backup(destination)
    assert os.path.exists(destination)
    copy_db = Database(destination).open()
    try:
        assert Repository(copy_db).count_games() == 4
        ok, message = copy_db.integrity_check()
        assert ok, message
    finally:
        copy_db.close_all()


def test_integrity_and_foreign_key_checks(db, repo, tmp_path):
    _seed_games(repo, tmp_path)
    ok, message = db.integrity_check()
    assert ok is True and message == "ok"
    assert db.foreign_key_check() == []


def test_counts_and_stats(db, repo, tmp_path):
    _seed_games(repo, tmp_path)
    counts = db.counts()
    assert counts["games"] == 4
    stats = repo.stats()
    assert stats["games"] == 4
    assert stats["favourites"] == 2
    assert stats["without_cover"] == 4
    assert stats["content_types"] >= 4


def test_scan_run_history(repo, tmp_path):
    root = repo.add_root(str(tmp_path))
    run_id = repo.start_scan_run(root.id, root.path)
    repo.finish_scan_run(
        run_id,
        {
            "folders_seen": 12, "new_folders": 3, "matched": 9, "unmatched": 3,
            "missing_folders": 1, "unavailable_drives": 0, "duration_ms": 42.5,
        },
    )
    runs = repo.recent_scan_runs()
    assert runs[0]["folders_seen"] == 12
    assert runs[0]["status"] == "done"
    assert runs[0]["duration_ms"] == 42.5


def test_op_samples_are_bounded(db, repo):
    for index in range(520):
        repo.record_sample("search", 1.0 + index, "detail")
    total = int(db.scalar("SELECT COUNT(*) FROM op_samples", default=0))
    assert total <= 500
    stats = repo.sample_stats()
    assert stats["search"]["n"] == total


def test_export_contains_no_credentials(repo, tmp_path):
    _seed_games(repo, tmp_path)
    repo.set_setting("theme", "dark")
    export = repo.export_collection()
    assert export["app"] == "Cartridge"
    assert len(export["games"]) == 4
    text = repr(export)
    for forbidden in ("client_secret", "api_key", "access_token"):
        assert forbidden not in text.lower()


# --------------------------------------------------------------------------
# writable-location handling (brief §7: never silently write elsewhere)
# --------------------------------------------------------------------------
def test_is_writable_location(tmp_path):
    ok, _message = is_writable_location(str(tmp_path))
    assert ok is True


def test_is_writable_location_rejects_a_file(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    ok, message = is_writable_location(str(f))
    assert ok is False
    assert "not a directory" in message


def test_is_writable_location_never_creates_by_default(tmp_path):
    """Probing must not litter the disk while the user is still deciding."""
    target = tmp_path / "deep" / "deeper"
    ok, message = is_writable_location(str(target))
    assert ok is False
    assert "does not exist" in message
    assert not target.exists()


def test_is_writable_location_can_create_when_asked(tmp_path):
    target = str(tmp_path / "deep" / "deeper")
    ok, _message = is_writable_location(target, create=True)
    assert ok is True
    assert os.path.isdir(target)


def test_fallback_path_is_outside_the_root(tmp_path):
    fallback = suggest_fallback_db_path(str(tmp_path))
    assert fallback.endswith("collection.db")
    assert not fallback.startswith(str(tmp_path))


def test_database_creates_its_own_directory(tmp_path):
    target = tmp_path / ".cartridge" / "collection.db"
    database = Database(str(target)).open()
    try:
        assert os.path.exists(str(target))
        assert database.integrity_check()[0]
    finally:
        database.close_all()


def test_database_in_unwritable_location_raises_clearly(tmp_path, monkeypatch):
    import cartridge.db.connection as conn_mod

    def deny(_path):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(conn_mod.os, "makedirs", deny)
    database = Database(str(tmp_path / "nope" / "collection.db"))
    with pytest.raises(DatabaseError) as info:
        database.open()
    assert "cannot create database directory" in str(info.value)
    assert "writable" in str(info.value)


def test_close_all_prevents_reuse(db):
    db.close_all()
    with pytest.raises(DatabaseError):
        _ = db.conn
