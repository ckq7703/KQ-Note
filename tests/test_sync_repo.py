import unittest

from tests.store_case import StoreCase
from app.sync import repo

ACCT = "me@example.com"


def remote(note_id="n1", rev=1, content="server text", deleted=False, purged=False, position="V",
           deleted_at=None, created_at="2026-01-01T00:00:00Z", updated_at="2026-01-02T00:00:00Z"):
    return {"id": note_id, "rev": rev, "content": content, "deleted": deleted, "purged": purged,
            "position": position, "deleted_at": deleted_at or ("2026-01-03T00:00:00Z" if deleted else None),
            "created_at": created_at, "updated_at": updated_at, "title": "", "seq": rev}


class RepoCase(StoreCase):
    def row(self, note_id="n1"):
        return repo.get_note(note_id)

    def pull(self, *changes, cursor=None):
        return repo.apply_page(ACCT, list(changes), cursor)

    def synced_note(self, note_id="n1", content="base", rev=1):
        """A note this device has fully synced at `rev`."""
        self.pull(remote(note_id, rev, content))
        return self.row(note_id)

    def edit_locally(self, note_id, content):
        self.store.set_scope(ACCT)
        self.store.save_note_by_id(note_id, content)
        self.store.set_scope(None)

    def contents(self):
        return sorted(r["content"] for r in self.sql("SELECT content FROM notes WHERE account_id = ?", (ACCT,)))


class ApplyRemoteTest(RepoCase):
    def test_a_new_remote_note_is_inserted_clean(self):
        events = self.pull(remote("n1", 3, "hello", position="Vk"))
        row = self.row()
        self.assertEqual((row["content"], row["server_rev"], row["dirty"], row["position"], row["server_position"]),
                         ("hello", 3, 0, "Vk", "Vk"))
        self.assertEqual(row["account_id"], ACCT)
        self.assertEqual(events, [{"type": "changed", "id": "n1"}])

    def test_remote_trashed_note_arrives_in_the_trash(self):
        self.pull(remote("n1", 2, "gone", deleted=True))
        row = self.row()
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["server_deleted"], 1)

    def test_purged_note_that_we_never_had_is_ignored(self):
        self.pull(remote("n1", 5, "", purged=True))
        self.assertIsNone(self.row())

    def test_newer_remote_replaces_a_clean_local_note(self):
        self.synced_note(content="v1", rev=1)
        self.pull(remote("n1", 2, "v2"))
        row = self.row()
        self.assertEqual((row["content"], row["server_rev"], row["dirty"]), ("v2", 2, 0))
        self.assertEqual(row["title"], "v2")

    def test_older_or_replayed_revisions_change_nothing(self):
        self.synced_note(content="v2", rev=2)
        self.assertEqual(self.pull(remote("n1", 1, "old")), [])
        self.assertEqual(self.pull(remote("n1", 2, "v2")), [])
        self.assertEqual(self.row()["content"], "v2")

    def test_the_cursor_is_saved_with_the_page(self):
        self.pull(remote("a", 1, "x"), cursor=7)
        self.assertEqual(repo.get_state(ACCT)["cursor"], 7)

    def test_a_foreign_scope_note_with_the_same_id_is_left_alone(self):
        self.store.create_note("local")
        local_id = self.store.list_notes()[0]["id"]
        self.pull(remote(local_id, 1, "from server"))
        self.assertEqual(self.store.load_note_by_id(local_id), "local")


class ConflictTest(RepoCase):
    def test_both_sides_edited_keeps_the_local_text_as_a_conflict_copy(self):
        self.synced_note(content="base", rev=1)
        self.edit_locally("n1", "my local edit")

        events = self.pull(remote("n1", 2, "their edit"))

        row = self.row()
        self.assertEqual((row["content"], row["dirty"], row["server_rev"]), ("their edit", 0, 2))
        conflicts = [e for e in events if e["type"] == "conflict"]
        self.assertEqual(len(conflicts), 1)
        copy = self.row(conflicts[0]["copy_id"])
        self.assertIn("my local edit", copy["content"])
        self.assertTrue(copy["content"].startswith("# [Xung đột]"))
        self.assertEqual((copy["dirty"], copy["server_rev"], copy["account_id"]), (1, 0, ACCT))
        self.assertEqual(sum(1 for c in self.contents() if "my local edit" in c), 1)  # nothing lost, nothing doubled

    def test_same_text_on_both_sides_is_not_a_conflict(self):
        self.synced_note(content="base", rev=1)
        self.edit_locally("n1", "same edit")
        events = self.pull(remote("n1", 2, "same edit"))
        row = self.row()
        self.assertEqual((row["dirty"], row["server_rev"]), (0, 2))
        self.assertFalse([e for e in events if e["type"] == "conflict"])
        self.assertEqual(len(self.contents()), 1)

    def test_conflict_copy_is_a_normal_note_that_gets_uploaded(self):
        self.synced_note(content="base", rev=1)
        self.edit_locally("n1", "local")
        copy_id = [e for e in self.pull(remote("n1", 2, "remote")) if e["type"] == "conflict"][0]["copy_id"]
        self.assertIn(copy_id, repo.push_candidates(ACCT))


class MergeTest(RepoCase):
    BASE = "title\nline a\nline b\nline c\nline d\n"

    def test_edits_in_different_places_merge_into_one_note(self):
        self.synced_note(content=self.BASE, rev=1)
        self.edit_locally("n1", "title\nline a\nline b\nline c\nline d\nadded here\n")
        events = self.pull(remote("n1", 2, "TITLE\nline a\nline b\nline c\nline d\n"))

        row = self.row()
        self.assertEqual(row["content"], "TITLE\nline a\nline b\nline c\nline d\nadded here\n")
        self.assertEqual((row["dirty"], row["server_rev"]), (1, 2))  # the merge still has to go up
        self.assertEqual(row["base_content"], "TITLE\nline a\nline b\nline c\nline d\n")
        self.assertTrue([e for e in events if e["type"] == "merged"])
        self.assertFalse([e for e in events if e["type"] == "conflict"])
        self.assertEqual(len(self.contents()), 1)  # no conflict copy
        self.assertEqual(repo.next_op(row), ("put", False))

    def test_edits_to_the_same_lines_still_make_a_conflict_copy(self):
        self.synced_note(content=self.BASE, rev=1)
        self.edit_locally("n1", "title\nOURS\nline b\nline c\nline d\n")
        events = self.pull(remote("n1", 2, "title\nTHEIRS\nline b\nline c\nline d\n"))
        self.assertTrue([e for e in events if e["type"] == "conflict"])
        self.assertEqual(self.row()["content"], "title\nTHEIRS\nline b\nline c\nline d\n")

    def test_when_the_merge_equals_the_servers_text_nothing_is_left_to_push(self):
        self.synced_note(content=self.BASE, rev=1)
        self.edit_locally("n1", "title\nline a\nline b\nline c\n")  # we deleted the last line
        self.pull(remote("n1", 2, "title\nline a\nline b\nline c\n"))  # so did they
        row = self.row()
        self.assertEqual((row["dirty"], row["server_rev"]), (0, 2))

    def test_our_edit_merged_into_a_note_the_server_deleted_brings_it_back(self):
        self.synced_note(content=self.BASE, rev=1)
        self.edit_locally("n1", "title\nline a\nline b\nline c\nline d\nmine\n")
        events = self.pull(remote("n1", 2, "TITLE\nline a\nline b\nline c\nline d\n", deleted=True))
        row = self.row()
        self.assertEqual(row["content"], "TITLE\nline a\nline b\nline c\nline d\nmine\n")
        self.assertIsNone(row["deleted_at"])
        self.assertTrue([e for e in events if e["type"] == "restored"])
        self.assertEqual(repo.next_op(row), ("put", True))

    def test_without_a_base_text_we_never_guess_and_keep_both(self):
        self.synced_note(content=self.BASE, rev=1)
        self.sql("UPDATE notes SET base_content = NULL WHERE id = 'n1'")
        self.edit_locally("n1", "title\nline a\nline b\nline c\nline d\nmine\n")
        events = self.pull(remote("n1", 2, "TITLE\nline a\nline b\nline c\nline d\n"))
        self.assertTrue([e for e in events if e["type"] == "conflict"])


class DeleteVsEditTest(RepoCase):
    def test_remote_delete_of_a_clean_note_trashes_it_here(self):
        self.synced_note(content="v1", rev=1)
        self.pull(remote("n1", 2, "v1", deleted=True))
        row = self.row()
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(row["server_deleted"], 1)

    def test_local_edit_beats_a_remote_delete(self):
        self.synced_note(content="v1", rev=1)
        self.edit_locally("n1", "edited here")
        events = self.pull(remote("n1", 2, "v1", deleted=True))
        row = self.row()
        self.assertEqual((row["content"], row["deleted_at"], row["dirty"], row["server_rev"]),
                         ("edited here", None, 1, 2))
        self.assertTrue([e for e in events if e["type"] == "restored"])
        self.assertEqual(repo.next_op(row), ("put", True))  # pushed with restore=True

    def test_both_sides_trashed_it_but_wrote_different_text_keeps_both(self):
        self.synced_note(content="v1", rev=1)
        self.edit_locally("n1", "my text")
        self.store.delete_note_by_id("n1")
        events = self.pull(remote("n1", 2, "their text", deleted=True))
        row = self.row()
        self.assertEqual((row["content"], row["dirty"]), ("their text", 0))
        self.assertIsNotNone(row["deleted_at"])  # the original stays in the trash with the server's text
        copies = [e for e in events if e["type"] == "conflict"]
        self.assertEqual(len(copies), 1)
        self.assertIn("my text", self.row(copies[0]["copy_id"])["content"])

    def test_both_sides_trashed_it_with_identical_text_is_not_a_conflict(self):
        self.synced_note(content="v1", rev=1)
        self.edit_locally("n1", "same text")
        self.store.delete_note_by_id("n1")
        events = self.pull(remote("n1", 2, "same text", deleted=True))
        self.assertFalse([e for e in events if e["type"] == "conflict"])
        row = self.row()
        self.assertEqual((row["dirty"], row["server_rev"]), (0, 2))
        self.assertIsNotNone(row["deleted_at"])

    def test_remote_edited_then_deleted_while_we_edited_keeps_both_texts(self):
        """The bug the fuzz test found: another device typed more AND deleted the note."""
        self.synced_note(content="base", rev=1)
        self.edit_locally("n1", "base\nmine")
        events = self.pull(remote("n1", 3, "base\ntheirs", deleted=True))
        row = self.row()
        self.assertEqual(row["content"], "base\ntheirs")  # the server's text is not thrown away
        copies = [e for e in events if e["type"] == "conflict"]
        self.assertEqual(len(copies), 1)
        self.assertIn("mine", self.row(copies[0]["copy_id"])["content"])

    def test_a_revision_bump_without_new_text_does_not_conflict_with_our_edit(self):
        self.synced_note(content="base", rev=1)
        self.edit_locally("n1", "base\nmine")
        events = self.pull(remote("n1", 2, "base"))  # e.g. trashed and restored elsewhere
        self.assertFalse([e for e in events if e["type"] == "conflict"])
        row = self.row()
        self.assertEqual((row["content"], row["dirty"], row["server_rev"]), ("base\nmine", 1, 2))
        self.assertEqual(len(self.contents()), 1)

    def test_both_sides_trashed_it_and_it_stays_trashed(self):
        self.synced_note(content="v1", rev=1)
        self.store.delete_note_by_id("n1")  # trashed here, unpushed
        self.pull(remote("n1", 2, "v1", deleted=True))
        row = self.row()
        self.assertIsNotNone(row["deleted_at"])
        self.assertIsNone(repo.next_op(row))

    def test_local_delete_loses_to_a_real_remote_edit(self):
        self.synced_note(content="v1", rev=1)
        self.store.set_scope(ACCT)
        self.store.create_note("keep one live")  # so deleting doesn't add a filler note
        self.store.delete_note_by_id("n1")
        self.store.set_scope(None)
        events = self.pull(remote("n1", 2, "edited elsewhere"))
        row = self.row()
        self.assertEqual((row["content"], row["deleted_at"]), ("edited elsewhere", None))
        self.assertTrue([e for e in events if e["type"] == "restored"])

    def test_local_delete_stands_when_the_revision_changed_but_not_the_text(self):
        self.synced_note(content="v1", rev=1)
        self.store.set_scope(ACCT)
        self.store.create_note("keep one live")
        self.store.delete_note_by_id("n1")
        self.store.set_scope(None)
        self.pull(remote("n1", 2, "v1"))  # e.g. trashed and restored elsewhere
        row = self.row()
        self.assertIsNotNone(row["deleted_at"])
        self.assertEqual(repo.next_op(row), ("trash",))

    def test_purged_remotely_while_clean_removes_it_and_keeps_a_disk_copy(self):
        self.synced_note(content="last words", rev=1)
        self.pull(remote("n1", 2, "", purged=True))
        self.assertIsNone(self.row())
        import os
        self.assertTrue([f for f in os.listdir(self.store.get_backups_dir()) if ".purged" in f])

    def test_purged_remotely_while_edited_locally_survives_as_a_new_note(self):
        self.synced_note(content="v1", rev=1)
        self.edit_locally("n1", "unsaved work")
        self.store.set_active_note_id("n1")
        self.pull(remote("n1", 2, "", purged=True))
        self.assertIsNone(self.row())
        new = self.sql("SELECT * FROM notes WHERE account_id = ? AND content = 'unsaved work'", (ACCT,))
        self.assertEqual(len(new), 1)
        self.assertEqual((new[0]["server_rev"], new[0]["dirty"]), (0, 1))
        self.assertNotEqual(new[0]["id"], "n1")
        self.assertEqual(self.sql("SELECT value FROM kv WHERE key = 'active_note_id'")[0][0], new[0]["id"])


class PositionTest(RepoCase):
    def test_a_move_from_another_device_is_adopted(self):
        self.synced_note()
        self.pull(remote("n1", 1, "base", position="Z"))
        self.assertEqual(self.row()["position"], "Z")

    def test_an_unpushed_local_move_is_kept_and_pushed_later(self):
        self.synced_note()
        self.sql("UPDATE notes SET position = 'C' WHERE id = 'n1'")
        self.pull(remote("n1", 1, "base", position="Z"))
        row = self.row()
        self.assertEqual((row["position"], row["server_position"]), ("C", "Z"))
        self.assertEqual(repo.next_op(row), ("move",))


class PushPlanTest(RepoCase):
    def test_next_op_table(self):
        base = {"deleted_at": None, "server_deleted": 0, "server_rev": 3, "dirty": 0, "position": "V",
                "server_position": "V", "content": "real text"}
        cases = [
            ({}, None),
            ({"dirty": 1}, ("put", False)),
            ({"server_rev": 0}, ("put", False)),
            ({"server_rev": 0, "deleted_at": 5}, ("put", False)),  # trashed before it ever synced: the trash still matches everywhere
            ({"server_rev": 0, "content": "# Ghi chú mới\n\nNội dung ghi chú..."}, None),  # untouched starter text stays local
            ({"server_rev": 0, "deleted_at": 5, "content": "  \n"}, None),
            ({"server_rev": 3, "dirty": 1, "content": "# Ghi chú mới\n"}, ("put", False)),  # but once synced, an edit down to starter text still goes up
            ({"deleted_at": 5}, ("trash",)),
            ({"deleted_at": 5, "dirty": 1}, ("put", False)),  # content first, then trash
            ({"server_deleted": 1}, ("restore",)),
            ({"server_deleted": 1, "dirty": 1}, ("put", True)),
            ({"deleted_at": 5, "server_deleted": 1}, None),
            ({"deleted_at": 5, "server_deleted": 1, "dirty": 1}, ("put", True)),  # text still has to reach the server
            ({"position": "C"}, ("move",)),
            ({"position": "C", "deleted_at": 5, "server_deleted": 1}, None),
        ]
        for changes, expected in cases:
            self.assertEqual(repo.next_op({**base, **changes}), expected, changes)

    def test_push_candidates_lists_only_notes_with_work_to_do(self):
        self.synced_note("clean", "c", 1)
        self.synced_note("edited", "e", 1)
        self.edit_locally("edited", "e2")
        self.store.set_scope(ACCT)
        new_id = self.store.create_note("brand new")
        self.store.set_scope(None)
        ids = repo.push_candidates(ACCT)
        self.assertIn("edited", ids)
        self.assertIn(new_id, ids)
        self.assertNotIn("clean", ids)

    def test_recording_a_push_keeps_the_note_dirty_if_the_user_kept_typing(self):
        self.synced_note("n1", "v1", 1)
        self.edit_locally("n1", "pushed text")
        self.edit_locally("n1", "pushed text and more")  # typed while the request was in flight
        repo.record_server_note("n1", remote("n1", 2, "pushed text"), pushed_content="pushed text")
        row = self.row()
        self.assertEqual((row["server_rev"], row["dirty"], row["base_content"]), (2, 1, "pushed text"))

    def test_recording_a_push_of_unchanged_text_clears_dirty(self):
        self.synced_note("n1", "v1", 1)
        self.edit_locally("n1", "pushed text")
        repo.record_server_note("n1", remote("n1", 2, "pushed text"), pushed_content="pushed text")
        self.assertEqual((self.row()["dirty"], self.row()["server_rev"]), (0, 2))

    def test_an_older_server_answer_is_ignored(self):
        self.synced_note("n1", "v3", 3)
        repo.record_server_note("n1", remote("n1", 2, "stale"))
        self.assertEqual(self.row()["server_rev"], 3)

    def test_reset_unsynced_makes_it_a_new_upload(self):
        self.synced_note("n1", "v1", 4)
        repo.reset_unsynced("n1")
        self.assertEqual(repo.next_op(self.row()), ("put", False))
        self.assertEqual(self.row()["server_rev"], 0)


class FullPassTest(RepoCase):
    def test_notes_the_server_forgot_are_reuploaded_not_dropped(self):
        self.synced_note("kept", "k", 1)
        self.synced_note("forgotten", "f", 1)
        repo.finish_full_pass(ACCT, {"kept"}, cursor=9)
        forgotten = self.row("forgotten")
        self.assertEqual((forgotten["server_rev"], forgotten["dirty"]), (0, 1))
        self.assertEqual(self.row("kept")["server_rev"], 1)
        self.assertEqual(repo.get_state(ACCT)["cursor"], 9)

    def test_a_forgotten_note_that_was_already_in_the_trash_is_purged(self):
        self.synced_note("t", "in trash", 1)
        self.sql("UPDATE notes SET deleted_at = 5 WHERE id = 't'")
        repo.finish_full_pass(ACCT, set(), cursor=1)
        self.assertIsNone(self.row("t"))

    def test_notes_never_synced_are_untouched(self):
        self.store.set_scope(ACCT)
        nid = self.store.create_note("only here")
        self.store.set_scope(None)
        repo.finish_full_pass(ACCT, set(), cursor=1)
        self.assertEqual(self.row(nid)["content"], "only here")


class LegacySlotTest(RepoCase):
    def test_imports_the_old_blob_unless_the_account_already_has_that_text(self):
        self.pull(remote("n1", 1, "COMMON COMMANDS\nls"))
        self.assertIsNone(repo.import_legacy_slot(ACCT, "COMMON COMMANDS\nls\n"))  # already migrated by the server
        self.assertEqual(len(self.contents()), 1)
        self.assertEqual(repo.get_state(ACCT)["legacy_imported"], 1)

    def test_a_blob_that_changed_since_the_server_migration_becomes_a_note(self):
        self.pull(remote("n1", 1, "old text"))
        created = repo.import_legacy_slot(ACCT, "newer text written by an old client")
        self.assertIn(created, repo.push_candidates(ACCT))
        self.assertEqual(len(self.contents()), 2)

    def test_an_empty_blob_only_marks_the_import_done(self):
        self.assertIsNone(repo.import_legacy_slot(ACCT, "  \n"))
        self.assertEqual(repo.get_state(ACCT)["legacy_imported"], 1)


if __name__ == "__main__":
    unittest.main()
