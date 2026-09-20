import unittest

from tests.widget_case import ACCT, WidgetCase, remote_note


class EditorMeetsSyncTest(WidgetCase):
    logged_in = True

    def setUp(self):
        super().setUp()
        from app.sync import repo
        repo.apply_page(ACCT, [remote_note("n1", 1, "alpha line")])
        self.open_widget()

    def test_starts_on_the_accounts_notes(self):
        self.assertEqual(self.store.get_scope(), ACCT)
        self.assertEqual(self.widget.active_note_id, "n1")
        self.assertEqual(self.editor_text(), "alpha line")

    def test_a_remote_change_is_shown_when_nothing_was_typed(self):
        self.remote(remote_note("n1", 2, "alpha changed remotely"))
        self.assertEqual(self.editor_text(), "alpha changed remotely")

    def test_the_cursor_stays_where_it_was_on_a_refresh(self):
        self.widget.text.mark_set("insert", "1.3")
        self.remote(remote_note("n1", 2, "alpha changed remotely"))
        self.assertEqual(self.widget.text.index("insert"), "1.3")

    def test_typing_during_a_remote_change_is_kept_as_a_conflict_copy(self):
        self.type_text(" MY TYPING")
        self.remote(remote_note("n1", 2, "alpha changed remotely"))
        self.assertIn("MY TYPING", self.editor_text())  # the user's screen is not yanked away mid-word

        self.widget.flush_save()  # the autosave timer fires

        self.assertEqual(self.store.load_note_by_id("n1"), "alpha changed remotely")  # not overwritten
        self.assertEqual(self.editor_text(), "alpha changed remotely")  # shows the original again, not the copy
        self.assertEqual(self.widget.active_note_id, "n1")
        copies = [r for r in self.account_notes() if r["content"].startswith("# [Xung đột]")]
        self.assertEqual(len(copies), 1)
        self.assertIn("MY TYPING", copies[0]["content"])
        self.assertEqual((copies[0]["dirty"], copies[0]["server_rev"]), (1, 0))  # will be uploaded
        self.messagebox.showinfo.assert_called_once()

    def test_an_arrow_key_after_a_remote_change_does_not_write_the_old_text_back(self):
        from app.sync import repo
        repo.apply_page(ACCT, [remote_note("n1", 2, "alpha changed remotely")])  # event not delivered yet
        self.widget._on_text_changed()  # e.g. a KeyRelease for an arrow key
        self.widget.flush_save()
        self.assertEqual(self.store.load_note_by_id("n1"), "alpha changed remotely")
        self.assertEqual(len(self.account_notes()), 1)  # no conflict copy out of thin air
        self.messagebox.showinfo.assert_not_called()

    def test_a_note_deleted_elsewhere_moves_the_editor_on_without_losing_typing(self):
        self.store.create_note("# another one")  # so there is somewhere to go
        self.widget._reload_active_note()
        self.store.set_active_note_id("n1")
        self.widget.active_note_id = "n1"
        self.widget._load_content_into_editor(self.store.load_note_by_id("n1"))
        self.type_text(" unsaved words")

        self.remote(remote_note("n1", 2, "alpha line", deleted=True))

        self.assertNotEqual(self.widget.active_note_id, "n1")
        self.assertNotIn("unsaved words", self.editor_text())
        trashed = [t for t in self.store.list_trash() if t["id"] == "n1"]
        self.assertEqual(len(trashed), 1)
        self.assertIn("unsaved words", self.store.load_note_by_id("n1"))  # recoverable from the trash
        self.messagebox.showinfo.assert_called()

    def test_the_only_note_deleted_elsewhere_leaves_a_fresh_note_to_type_in(self):
        self.remote(remote_note("n1", 2, "alpha line", deleted=True))
        self.assertTrue(self.widget.active_note_id)
        self.assertNotEqual(self.widget.active_note_id, "n1")
        self.assertEqual(len(self.store.list_notes()), 1)

    def test_conflicts_reported_by_the_engine_are_announced(self):
        self.widget._on_notes_changed({"changed": ["n1"], "restored": [],
                                      "conflicts": [{"id": "n1", "copy_id": "c", "title": "Shopping"}]})
        args = self.messagebox.showinfo.call_args[0]
        self.assertIn("hai nơi", args[0])
        self.assertIn("Shopping", args[1])

    def test_typing_saves_and_schedules_a_sync(self):
        self.type_text(" more")
        self.widget.flush_save()
        self.assertEqual(self.store.load_note_by_id("n1"), "alpha line more")
        self.assertIsNotNone(self.widget._sync_after_id)
        self.widget._run_sync()
        self.sync_async.assert_called()

    def test_structural_changes_schedule_a_sync_too(self):
        self.widget._sync_after_id = None
        self.widget._confirm_delete_note  # exists
        self.store.delete_note_by_id("n1")
        self.widget._on_trash_changed()
        self.assertIsNotNone(self.widget._sync_after_id)

    def test_status_glyph_follows_the_engine_state(self):
        engine = self.widget.sync_engine
        for state, colour in (("synced", "#6fd0a0"), ("offline", "#d9a441"), ("error", "#e5706b"), ("syncing", "#5b9df0")):
            engine._status.update(state=state, message="x", pending=0)
            self.widget._update_cloud_icon()
            self.assertEqual(str(self.widget.sync_status_lbl.cget("text")), "☁")
            self.assertEqual(str(self.widget.sync_status_lbl.cget("fg")), colour, state)
        engine._status.update(state="synced", pending=3, last_ok=1_700_000_000)  # synced, but work is waiting
        self.widget._update_cloud_icon()
        self.assertEqual(str(self.widget.sync_status_lbl.cget("fg")), "#d9a441")
        self.assertIn("3", self.widget._sync_status_text())

    def test_logging_out_returns_to_the_local_notes(self):
        self.store.set_scope(None)
        local = self.store.create_note("local only text")
        self.store.set_scope(ACCT)
        self.widget._logout()
        self.assertIsNone(self.store.get_scope())
        self.assertIn(local, [n["id"] for n in self.store.list_notes()])
        self.assertEqual(str(self.widget.sync_status_lbl.cget("text")), "")
        self.assertIn("n1", [r["id"] for r in self.account_notes()])  # the account's notes stay on disk


class LoginFlowTest(WidgetCase):
    logged_in = False

    def test_signing_in_switches_to_the_account_once_it_is_ready(self):
        self.store.create_note("local note text")
        self.open_widget()
        typed = "typed just before login"
        self.type_text(typed)

        self.auth_state["logged_in"] = True
        self.widget._on_login_success()  # flushes typing, asks the engine to sync
        self.sync_async.assert_called()
        self.assertIn(typed, self.store.load_note_by_id(self.widget.active_note_id))  # saved locally first

        from app.sync import repo
        repo.apply_page(ACCT, [remote_note("srv1", 1, "note from the account")])
        self.widget._on_account_ready(ACCT)

        self.assertEqual(self.store.get_scope(), ACCT)
        self.assertEqual([n["title"] for n in self.store.list_notes()], ["note from the account"])
        self.assertEqual(self.editor_text(), "note from the account")

    def test_account_ready_while_already_showing_the_account_changes_nothing(self):
        from app.sync import repo
        repo.apply_page(ACCT, [remote_note("n1", 1, "alpha")])
        self.logged_in = True
        self.open_widget()
        self.type_text(" typing in progress")
        self.widget._on_account_ready(ACCT)
        self.assertIn("typing in progress", self.editor_text())  # not reloaded under the user's hands


if __name__ == "__main__":
    unittest.main()
