import os
import sqlite3
import threading
import unittest
from unittest import mock

from tests.store_case import StoreCase


class MigrationTest(StoreCase):
    def test_fresh_install_gets_one_default_note(self):
        notes = self.store.list_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(self.store.load_note_by_id(notes[0]["id"]), self.store.DEFAULT_CONTENT)
        self.assertEqual(self.store.get_active_note_id(), notes[0]["id"])

    def test_imports_file_layout_in_order_and_maps_the_active_note(self):
        self.write_legacy(
            [("note_default", "# One\nfirst"), ("note_a1b2c3d4", "# Two\nsecond"), ("note_ffff0000", "# Three")],
            active="note_a1b2c3d4",
            index_extra={"gemini_api_key": "KEY", "selected_gemini_model": "gemini-x"},
        )
        self.restart()

        notes = self.store.list_notes()
        self.assertEqual([self.store.load_note_by_id(n["id"]) for n in notes],
                         ["# One\nfirst", "# Two\nsecond", "# Three"])
        self.assertEqual(self.store.load_note_by_id(self.store.get_active_note_id()), "# Two\nsecond")
        self.assertTrue(all(len(n["id"]) == 36 for n in notes))  # real UUIDs, not note_default
        self.assertEqual(self.store.get_gemini_api_key(), "KEY")
        self.assertEqual(self.store.get_selected_gemini_model(), "gemini-x")
        self.assertEqual(notes[0]["created_at"], 1_690_000_000)  # original timestamps kept
        self.assertEqual(notes[0]["title"], "One")

    def test_old_files_are_left_untouched(self):
        store_dir = self.write_legacy([("note_default", "keep me"), ("note_b", "and me")], active="note_b")
        before = self.snapshot_dir(store_dir)
        self.restart()
        self.store.list_notes()
        self.assertEqual(self.snapshot_dir(store_dir), before)

    def test_import_happens_once(self):
        self.write_legacy([("note_default", "old")])
        self.restart()
        new_id = self.store.create_note("# Created after import")
        self.restart()
        contents = sorted(self.store.load_note_by_id(n["id"]) for n in self.store.list_notes())
        self.assertEqual(contents, ["# Created after import", "old"])
        self.assertEqual(self.store.get_active_note_id(), new_id)

    def test_corrupt_index_is_recovered_from_the_note_files(self):
        store_dir = self.write_legacy([("note_default", "alpha"), ("note_b", "beta")], write_index=False)
        with open(os.path.join(store_dir, "index.json"), "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.restart()

        self.assertEqual(sorted(self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()),
                         ["alpha", "beta"])
        self.assertTrue(any(n.startswith("index.corrupt-") for n in os.listdir(store_dir)))

    def test_note_files_missing_from_the_index_are_imported_after_it(self):
        store_dir = self.write_legacy([("note_a", "indexed")])
        with open(os.path.join(store_dir, "note_orphan.txt"), "w", encoding="utf-8") as f:
            f.write("orphan text")
        with open(os.path.join(store_dir, "note_blank.txt"), "w", encoding="utf-8") as f:
            f.write("  \n")
        self.restart()
        self.assertEqual([self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()],
                         ["indexed", "orphan text"])

    def test_indexed_note_whose_file_is_gone_stays_as_an_empty_note(self):
        store_dir = self.write_legacy([("note_a", "here"), ("note_b", "vanishing")])
        os.remove(os.path.join(store_dir, "note_b.txt"))
        self.restart()
        self.assertEqual([self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()], ["here", ""])

    def test_imports_single_note_and_oldest_database_layouts(self):
        with open(os.path.join(self.data_dir, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("from notes.txt")
        self.restart()
        self.assertEqual([self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()], ["from notes.txt"])

    def test_imports_the_oldest_notes_db(self):
        conn = sqlite3.connect(os.path.join(self.data_dir, "notes.db"))
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, content TEXT)")
        conn.executemany("INSERT INTO notes (content) VALUES (?)", [("block one",), ("",), ("block two",)])
        conn.commit()
        conn.close()
        self.restart()
        self.assertEqual([self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()],
                         ["block one\n\nblock two\n"])

    def test_a_failed_import_changes_nothing_and_can_be_retried(self):
        store_dir = self.write_legacy([("note_default", "precious")])
        before = self.snapshot_dir(store_dir)

        with mock.patch.object(self.store.fracindex, "n_keys_between", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.store.initialize()

        self.assertEqual(self.snapshot_dir(store_dir), before)
        conn = sqlite3.connect(self.store.get_db_path())
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'notes'").fetchone()[0], 0)
        finally:
            conn.close()

        self.store.initialize()  # the retry succeeds
        self.assertEqual([self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()], ["precious"])


class NotesTest(StoreCase):
    def test_create_puts_the_newest_note_first_and_makes_it_active(self):
        a = self.store.create_note("# A")
        b = self.store.create_note("# B")
        ids = [n["id"] for n in self.store.list_notes()]
        self.assertEqual(ids[:2], [b, a])
        self.assertEqual(self.store.get_active_note_id(), b)

    def test_save_updates_content_title_and_snippet(self):
        nid = self.store.create_note("# Old")
        self.store.save_note_by_id(nid, "## New title\nsome body")
        note = next(n for n in self.store.list_notes() if n["id"] == nid)
        self.assertEqual(note["title"], "New title")
        self.assertIn("some body", note["snippet"])
        self.assertEqual(self.store.load_note_by_id(nid), "## New title\nsome body")

    def test_saving_identical_content_is_not_an_edit(self):
        nid = self.store.create_note("same")
        self.sql("UPDATE notes SET updated_at = 5 WHERE id = ?", (nid,))
        self.store.save_note_by_id(nid, "same")
        self.assertEqual(self.sql("SELECT updated_at FROM notes WHERE id = ?", (nid,))[0][0], 5)

    def test_saving_an_unknown_id_creates_the_note(self):
        self.store.save_note_by_id("brand-new", "hello")
        self.assertEqual(self.store.load_note_by_id("brand-new"), "hello")
        self.assertIn("brand-new", [n["id"] for n in self.store.list_notes()])

    def test_unicode_and_large_content_round_trip(self):
        nid = self.store.create_note("x")
        text = "Ghi chú tiếng Việt: đường, ơ, ư ✓ 🚀\n" * 30000  # about 1 MB
        self.store.save_note_by_id(nid, text)
        self.assertEqual(self.store.load_note_by_id(nid), text)

    def test_data_survives_a_restart(self):
        nid = self.store.create_note("# Persist")
        self.store.set_gemini_api_key("abc")
        self.restart()
        self.assertEqual(self.store.load_note_by_id(nid), "# Persist")
        self.assertEqual(self.store.get_active_note_id(), nid)
        self.assertEqual(self.store.get_gemini_api_key(), "abc")

    def test_a_failure_inside_a_write_rolls_the_whole_write_back(self):
        before = len(self.store.list_notes())
        with mock.patch.object(self.store, "_kv_set", side_effect=RuntimeError("disk on fire")):
            with self.assertRaises(RuntimeError):
                self.store.create_note("# Never stored")
        self.assertEqual(len(self.store.list_notes()), before)

    def test_concurrent_writers_do_not_lose_notes_or_error(self):
        errors = []

        def worker(tag):
            try:
                for i in range(25):
                    self.store.create_note(f"{tag}-{i}")
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in "abcd"]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.store.list_notes()), 1 + 100)  # + the default note

    def test_positions_stay_short_after_many_creations(self):
        for i in range(120):
            self.store.create_note(f"note {i}")
        positions = [r[0] for r in self.sql("SELECT position FROM notes WHERE deleted_at IS NULL")]
        self.assertLessEqual(max(len(p) for p in positions), self.store.MAX_KEY_LEN)
        titles = [n["title"] for n in self.store.list_notes()]
        self.assertEqual(titles[:3], ["note 119", "note 118", "note 117"])  # order survived renumbering


class ReorderTest(StoreCase):
    def setUp(self):
        super().setUp()
        for i in range(5):
            self.store.create_note(f"# n{i}")
        # newest first, followed by the note every fresh install starts with
        self.default_first = [n["id"] for n in self.store.list_notes()]
        self.assertEqual(len(self.default_first), 6)

    def positions(self):
        return {r["id"]: r["position"] for r in self.sql("SELECT id, position FROM notes")}

    def test_a_single_move_rewrites_only_that_notes_position(self):
        order = self.default_first
        before = self.positions()
        moved = order[-1]
        new_order = [moved] + order[:-1]  # drag the last note to the top

        self.store.reorder_notes(new_order)

        self.assertEqual([n["id"] for n in self.store.list_notes()], new_order)
        after = self.positions()
        self.assertEqual([i for i in before if before[i] != after[i]], [moved])

    def test_moving_into_the_middle(self):
        order = self.default_first
        new_order = [order[1], order[2], order[0]] + order[3:]
        self.store.reorder_notes(new_order)
        self.assertEqual([n["id"] for n in self.store.list_notes()], new_order)

    def test_a_multi_note_reshuffle_is_applied_too(self):
        new_order = list(reversed(self.default_first))
        self.store.reorder_notes(new_order)
        self.assertEqual([n["id"] for n in self.store.list_notes()], new_order)

    def test_notes_not_mentioned_keep_their_order_at_the_end(self):
        order = self.default_first
        self.store.reorder_notes([order[2], order[1]])
        self.assertEqual([n["id"] for n in self.store.list_notes()],
                         [order[2], order[1], order[0]] + order[3:])

    def test_unknown_and_duplicate_ids_are_ignored(self):
        order = self.default_first
        self.store.reorder_notes(["nope", order[1], order[1], order[0]] + order[2:])
        self.assertEqual([n["id"] for n in self.store.list_notes()][:2], [order[1], order[0]])

    def test_foreign_or_duplicate_position_keys_are_repaired(self):
        self.sql("UPDATE notes SET position = 'a0'")  # e.g. keys written by another writer
        order = [n["id"] for n in self.store.list_notes()]
        self.store.reorder_notes([order[-1]] + order[:-1])
        self.assertEqual([n["id"] for n in self.store.list_notes()][0], order[-1])
        self.assertEqual(len(set(self.positions().values())), len(order))


class TrashTest(StoreCase):
    def test_delete_moves_a_note_to_the_trash_and_keeps_its_content(self):
        keep = self.store.create_note("# keep")
        gone = self.store.create_note("# gone\nbody")
        self.store.delete_note_by_id(gone)

        self.assertEqual([n["id"] for n in self.store.list_notes()][0], keep)
        self.assertNotIn(gone, [n["id"] for n in self.store.list_notes()])
        trashed = self.store.list_trash()
        self.assertEqual([t["id"] for t in trashed], [gone])
        self.assertEqual(self.store.load_note_by_id(gone), "# gone\nbody")

    def test_deleting_the_active_note_activates_another(self):
        other = self.store.create_note("# other")
        active = self.store.create_note("# active")
        self.assertEqual(self.store.delete_note_by_id(active), other)
        self.assertEqual(self.store.get_active_note_id(), other)

    def test_deleting_the_last_note_leaves_a_fresh_one(self):
        for n in self.store.list_notes():
            new_active = self.store.delete_note_by_id(n["id"])
        self.assertEqual(len(self.store.list_notes()), 1)
        self.assertEqual(self.store.get_active_note_id(), new_active)
        self.assertEqual(self.store.load_note_by_id(new_active), "# Ghi chú mới\n")

    def test_restore_puts_the_note_back_on_top(self):
        a = self.store.create_note("# a")
        self.store.create_note("# b")
        self.store.delete_note_by_id(a)
        self.assertTrue(self.store.restore_note(a))
        self.assertEqual(self.store.list_notes()[0]["id"], a)
        self.assertEqual(self.store.list_trash(), [])
        self.assertFalse(self.store.restore_note(a))  # already live

    def test_purge_deletes_for_good_but_leaves_a_disk_copy(self):
        nid = self.store.create_note("# last words")
        self.store.delete_note_by_id(nid)
        self.assertTrue(self.store.purge_note(nid))

        self.assertEqual(self.sql("SELECT count(*) FROM notes WHERE id = ?", (nid,))[0][0], 0)
        backups = [f for f in os.listdir(self.store.get_backups_dir()) if ".purged" in f]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.store.get_backups_dir(), backups[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# last words")

    def test_a_live_note_cannot_be_purged(self):
        nid = self.store.create_note("# live")
        self.assertFalse(self.store.purge_note(nid))
        self.assertEqual(self.store.load_note_by_id(nid), "# live")

    def test_empty_trash(self):
        a, b = self.store.create_note("# a"), self.store.create_note("# b")
        self.store.delete_note_by_id(a)
        self.store.delete_note_by_id(b)
        self.assertEqual(self.store.empty_trash(), 2)
        self.assertEqual(self.store.list_trash(), [])

    def test_only_notes_trashed_longer_than_the_retention_window_expire(self):
        old, recent = self.store.create_note("# old"), self.store.create_note("# recent")
        self.store.create_note("# live")
        self.store.delete_note_by_id(old)
        self.store.delete_note_by_id(recent)
        day = 86400
        self.sql("UPDATE notes SET deleted_at = ? WHERE id = ?", (1_000_000, old))
        self.sql("UPDATE notes SET deleted_at = ? WHERE id = ?", (1_000_000 + 30 * day, recent))

        purged = self.store.purge_expired_trash(now=1_000_000 + (self.store.TRASH_RETENTION_DAYS + 1) * day)

        self.assertEqual(purged, 1)
        self.assertEqual([t["id"] for t in self.store.list_trash()], [recent])

    def test_expired_trash_is_purged_on_startup(self):
        nid = self.store.create_note("# ancient")
        self.store.delete_note_by_id(nid)
        self.sql("UPDATE notes SET deleted_at = 1 WHERE id = ?", (nid,))
        self.restart()
        self.store.initialize()
        self.assertEqual(self.store.list_trash(), [])

    def test_saving_to_a_trashed_note_does_not_resurrect_it(self):
        nid = self.store.create_note("# v1")
        self.store.delete_note_by_id(nid)
        self.store.save_note_by_id(nid, "# v2")
        self.assertEqual([t["id"] for t in self.store.list_trash()], [nid])
        self.assertNotIn(nid, [n["id"] for n in self.store.list_notes()])


class AccountScopeTest(StoreCase):
    def test_scopes_are_separate_lists(self):
        local = self.store.create_note("# local")
        self.store.set_scope("acct-1")
        self.assertEqual(self.store.list_notes(), [])
        self.assertIsNone(self.store.get_active_note_id())
        mine = self.store.create_note("# mine")
        self.assertEqual([n["id"] for n in self.store.list_notes()], [mine])
        self.store.set_scope(None)
        self.assertIn(local, [n["id"] for n in self.store.list_notes()])
        self.assertNotIn(mine, [n["id"] for n in self.store.list_notes()])

    def test_active_note_from_another_scope_is_ignored(self):
        self.store.create_note("# local")
        self.store.set_scope("acct-1")
        self.assertIsNone(self.store.get_active_note_id())  # not the local note
        self.assertEqual(self.store.load_content(), self.store.DEFAULT_CONTENT)

    def test_edits_mark_account_notes_dirty_but_not_local_ones(self):
        local = self.store.create_note("# l")
        self.store.set_scope("acct-1")
        mine = self.store.create_note("# m")
        self.sql("UPDATE notes SET dirty = 0")
        self.store.save_note_by_id(mine, "# m2")
        self.store.set_scope(None)
        self.store.save_note_by_id(local, "# l2")
        dirty = {r["id"]: r["dirty"] for r in self.sql("SELECT id, dirty FROM notes")}
        self.assertEqual((dirty[mine], dirty[local]), (1, 0))

    def test_copy_local_notes_to_account(self):
        a = self.store.create_note("# a")
        b = self.store.create_note("# b")
        blank = self.store.create_note(" \n\t\n")
        trashed = self.store.create_note("# trashed")
        self.store.delete_note_by_id(trashed)
        local_before = {n["id"]: self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()}

        copied = self.store.copy_local_notes_to_account("acct-1")

        # the blank note and the untouched starter note are not worth uploading
        real = {i: c for i, c in local_before.items() if i != blank and c != self.store.DEFAULT_CONTENT}
        self.assertEqual(copied, len(real))
        self.store.set_scope("acct-1")
        copies = self.store.list_notes()
        self.assertEqual(sorted(self.store.load_note_by_id(n["id"]) for n in copies), sorted(real.values()))
        self.assertTrue(set(n["id"] for n in copies).isdisjoint(local_before))  # brand-new ids
        state = self.sql("SELECT dirty, server_rev FROM notes WHERE account_id = 'acct-1'")
        self.assertTrue(all((r["dirty"], r["server_rev"]) == (1, 0) for r in state))
        self.store.set_scope(None)  # originals are still there for when the user logs out
        self.assertEqual({n["id"] for n in self.store.list_notes()}, set(local_before))
        self.assertNotIn(trashed, [c["id"] for c in copies])
        self.assertIn(a, local_before)
        self.assertIn(b, local_before)

    def test_copying_is_idempotent_per_account(self):
        self.store.create_note("# one")
        first = self.store.copy_local_notes_to_account("acct-1")
        self.assertEqual(first, 1)
        self.assertEqual(self.store.copy_local_notes_to_account("acct-1"), 0)  # logging in again
        self.store.create_note("# two")
        self.assertEqual(self.store.copy_local_notes_to_account("acct-1"), 1)  # only the new one
        self.assertEqual(self.store.copy_local_notes_to_account("acct-2"), first + 1)  # other account gets all

    def test_starter_text_is_uploaded_only_after_the_user_edits_it(self):
        starter = self.store.create_note("# Ghi chú mới\n\nNội dung ghi chú...")
        self.assertEqual(self.store.copy_local_notes_to_account("acct-1"), 0)
        self.store.save_note_by_id(starter, "# Ghi chú mới\n\nNow with real content")
        self.assertEqual(self.store.copy_local_notes_to_account("acct-1"), 1)

    def test_skip_duplicates_only_remembers_notes_the_account_already_has(self):
        self.store.set_scope("acct-1")
        self.store.create_note("# already on the account")
        self.store.set_scope(None)
        self.store.create_note("# already on the account")
        self.store.create_note("# only local")
        self.assertEqual(self.store.copy_local_notes_to_account("acct-1", skip_duplicates=True), 1)
        self.assertEqual(self.store.copy_local_notes_to_account("acct-1", skip_duplicates=True), 0)
        self.store.set_scope("acct-1")
        self.assertEqual(sorted(self.store.load_note_by_id(n["id"]) for n in self.store.list_notes()),
                         ["# already on the account", "# only local"])


class SchemaUpgradeTest(StoreCase):
    V1_SCHEMA = [
        """CREATE TABLE notes (id TEXT PRIMARY KEY, account_id TEXT, content TEXT NOT NULL DEFAULT '',
           title TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '', position TEXT NOT NULL DEFAULT '',
           created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER, legacy_id TEXT,
           server_rev INTEGER NOT NULL DEFAULT 0, base_content TEXT, dirty INTEGER NOT NULL DEFAULT 0)""",
        "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT)",
        "CREATE TABLE adoptions (local_id TEXT NOT NULL, account_id TEXT NOT NULL, PRIMARY KEY (local_id, account_id))",
    ]

    def test_a_version_1_database_is_upgraded_in_place_without_touching_notes(self):
        conn = sqlite3.connect(self.store.get_db_path())
        for statement in self.V1_SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT INTO notes (id, content, position, created_at, updated_at) VALUES ('n1', 'kept', 'V', 1, 2)")
        conn.execute("INSERT INTO kv VALUES ('active_note_id', 'n1')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()

        self.restart()
        self.store.initialize()

        self.assertEqual(self.store.load_note_by_id("n1"), "kept")
        self.assertEqual(self.store.get_active_note_id(), "n1")
        row = self.sql("SELECT server_deleted, server_position FROM notes WHERE id = 'n1'")[0]
        self.assertEqual((row[0], row[1]), (0, None))
        self.assertEqual(self.sql("SELECT count(*) FROM sync_state")[0][0], 0)  # the table exists
        self.assertEqual(self.sql("PRAGMA user_version")[0][0], self.store.SCHEMA_VERSION)


class CompareAndSaveTest(StoreCase):
    def test_saving_over_what_the_editor_loaded_is_a_normal_save(self):
        nid = self.store.create_note("v1")
        self.assertIsNone(self.store.save_note_by_id(nid, "v2", expected_old="v1"))
        self.assertEqual(self.store.load_note_by_id(nid), "v2")

    def test_if_the_text_changed_underneath_the_editor_text_becomes_a_conflict_copy(self):
        nid = self.store.create_note("v1")
        self.store.save_note_by_id(nid, "v2 applied by a sync")  # the editor still thinks it is v1

        outcome = self.store.save_note_by_id(nid, "v1 plus my typing", expected_old="v1")
        copy_id = outcome.copy_id
        self.assertFalse(outcome.merged)

        self.assertEqual(self.store.load_note_by_id(nid), "v2 applied by a sync")  # not overwritten
        copy = self.store.load_note_by_id(copy_id)
        self.assertTrue(copy.startswith("# [Xung đột]"))
        self.assertIn("v1 plus my typing", copy)
        self.assertEqual(self.store.list_notes()[0]["id"], copy_id)  # visible at the top of the list

    def test_a_copy_lands_in_the_notes_own_account_not_the_visible_scope(self):
        self.store.set_scope("acct-1")
        nid = self.store.create_note("v1")
        self.store.save_note_by_id(nid, "v2")
        copy_id = self.store.save_note_by_id(nid, "mine", expected_old="v1").copy_id
        row = self.sql("SELECT account_id, dirty, server_rev FROM notes WHERE id = ?", (copy_id,))[0]
        self.assertEqual(tuple(row), ("acct-1", 1, 0))

    def test_text_already_equal_to_the_stored_text_is_never_a_conflict(self):
        nid = self.store.create_note("same")
        self.assertIsNone(self.store.save_note_by_id(nid, "same", expected_old="stale"))
        self.assertEqual(len(self.store.list_notes()), 2)  # + the fresh-install note, no copy

    def test_changes_in_different_places_are_merged_not_turned_into_a_conflict(self):
        nid = self.store.create_note("first\nsecond\nthird\n")
        self.store.save_note_by_id(nid, "FIRST (from a sync)\nsecond\nthird\n")  # the editor still has the old text

        outcome = self.store.save_note_by_id(nid, "first\nsecond\nthird\nmy new line\n", expected_old="first\nsecond\nthird\n")

        self.assertTrue(outcome.merged)
        self.assertIsNone(outcome.copy_id)
        self.assertEqual(self.store.load_note_by_id(nid), "FIRST (from a sync)\nsecond\nthird\nmy new line\n")
        self.assertEqual(len(self.store.list_notes()), 2)  # no conflict copy appeared

    def test_a_merge_marks_an_account_note_dirty_so_it_uploads(self):
        self.store.set_scope("acct-1")
        nid = self.store.create_note("a\nb\nc\n")
        self.sql("UPDATE notes SET dirty = 0 WHERE id = ?", (nid,))
        self.store.save_note_by_id(nid, "A\nb\nc\n")
        self.sql("UPDATE notes SET dirty = 0 WHERE id = ?", (nid,))
        self.store.save_note_by_id(nid, "a\nb\nc\nmine\n", expected_old="a\nb\nc\n")
        self.assertEqual(self.sql("SELECT dirty FROM notes WHERE id = ?", (nid,))[0][0], 1)

    def test_without_expected_old_the_save_is_unconditional(self):
        nid = self.store.create_note("v1")
        self.store.save_note_by_id(nid, "v2")
        self.assertIsNone(self.store.save_note_by_id(nid, "v3"))
        self.assertEqual(self.store.load_note_by_id(nid), "v3")


class EnsureNoteTest(StoreCase):
    def test_an_empty_scope_gets_one_fresh_note(self):
        self.store.set_scope("acct-1")
        active = self.store.ensure_note()
        self.assertEqual(self.store.load_note_by_id(active), "# Ghi chú mới\n")
        self.assertEqual(self.store.ensure_note(), active)  # and only one


if __name__ == "__main__":
    unittest.main()
