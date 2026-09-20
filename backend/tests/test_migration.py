"""The v1 -> v2 schema migration, run against a database shaped exactly like production was."""

import os
import tempfile

from sqlalchemy import create_engine, inspect, text

from app.migrations import migrate_single_note_to_multi, run_startup_migrations


def _v1_database():
    path = os.path.join(tempfile.mkdtemp(), "v1.db")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL UNIQUE, "
                       "password_hash VARCHAR, google_sub VARCHAR UNIQUE, avatar_url VARCHAR, "
                       "created_at DATETIME NOT NULL)"))
        c.execute(text("CREATE TABLE notes (user_id INTEGER PRIMARY KEY REFERENCES users(id), "
                       "content TEXT NOT NULL, version INTEGER NOT NULL, updated_at DATETIME NOT NULL, "
                       "updated_by_device VARCHAR)"))
        c.execute(text("INSERT INTO users VALUES (2, 'a@x.com', NULL, 'g2', NULL, '2026-08-15 11:50:56')"))
        c.execute(text("INSERT INTO users VALUES (4, 'b@x.com', NULL, 'g4', NULL, '2026-09-09 07:55:19')"))
        c.execute(text("INSERT INTO users VALUES (5, 'c@x.com', NULL, 'g5', NULL, '2026-09-09 07:55:19')"))
        c.execute(text("INSERT INTO notes VALUES (2, '# WORK NOTES\nrepo list', 902, '2026-09-10 03:12:01', 'dev-a')"))
        c.execute(text("INSERT INTO notes VALUES (4, '', 4, '2026-09-09 08:32:04', 'dev-b')"))
    return engine


def test_migration_keeps_legacy_table_and_copies_non_empty_blobs():
    engine = _v1_database()

    assert migrate_single_note_to_multi(engine) == 1

    insp = inspect(engine)
    assert insp.has_table("notes_legacy")
    assert "id" in {c["name"] for c in insp.get_columns("notes")}
    with engine.connect() as c:
        legacy = c.execute(text("SELECT user_id, content, version FROM notes_legacy ORDER BY user_id")).all()
        assert [(r[0], r[2]) for r in legacy] == [(2, 902), (4, 4)]  # untouched, incl. empty row

        rows = c.execute(text("SELECT user_id, id, title, content, rev, seq, position, updated_by_device "
                              "FROM notes")).all()
        assert len(rows) == 1
        user_id, note_id, title, content, rev, seq, position, device = rows[0]
        assert (user_id, title, content, rev, seq, device) == (2, "WORK NOTES", "# WORK NOTES\nrepo list", 1, 1, "dev-a")
        assert len(note_id) == 36 and position
        sync = c.execute(text("SELECT user_id, seq FROM user_sync")).all()
        assert [tuple(r) for r in sync] == [(2, 1)]


def test_migration_is_idempotent_and_startup_creates_the_rest():
    engine = _v1_database()
    assert migrate_single_note_to_multi(engine) == 1
    assert migrate_single_note_to_multi(engine) == 0  # already v2

    run_startup_migrations(engine)
    for table in ("note_revisions", "user_sync", "images", "notes_legacy", "notes"):
        assert inspect(engine).has_table(table), table
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM notes")).scalar() == 1


def test_fresh_database_needs_no_migration():
    engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(), 'fresh.db')}")
    assert migrate_single_note_to_multi(engine) == 0
    run_startup_migrations(engine)
    assert inspect(engine).has_table("notes_legacy") and inspect(engine).has_table("notes")


def test_refuses_to_guess_when_both_old_and_legacy_tables_exist():
    engine = _v1_database()
    with engine.begin() as c:
        c.execute(text("CREATE TABLE notes_legacy (x INTEGER)"))
    try:
        migrate_single_note_to_multi(engine)
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError")
