import tkinter as tk
import unittest
from unittest import mock

from tests.store_case import StoreCase


def _walk(widget):
    for child in widget.winfo_children():
        yield child
        yield from _walk(child)


def _buttons(widget, text):
    return [w for w in _walk(widget) if w.winfo_class() == "Button" and w.cget("text") == text]


class TrashDialogTest(StoreCase):
    def setUp(self):
        super().setUp()
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display available: {e}")
        self.root.withdraw()
        from app import trash_dialog
        self.trash_dialog = trash_dialog

    def tearDown(self):
        if hasattr(self, "root"):
            self.root.destroy()
        super().tearDown()

    def _dialog(self, **kw):
        dialog = self.trash_dialog.TrashDialog(self.root, **kw)
        dialog.update()
        return dialog

    def test_empty_trash_shows_a_message_and_disables_the_clear_button(self):
        dialog = self._dialog()
        labels = [w.cget("text") for w in _walk(dialog) if w.winfo_class() == "Label"]
        self.assertIn("Thùng rác trống.", labels)
        self.assertEqual(str(dialog._empty_btn.cget("state")), "disabled")

    def test_lists_trashed_notes_and_restores_one(self):
        keep = self.store.create_note("# stays deleted\nx")
        back = self.store.create_note("# bring me back\ny")
        self.store.delete_note_by_id(keep)
        self.store.delete_note_by_id(back)
        # deleted_at has one-second resolution; pin the order the dialog should show
        self.sql("UPDATE notes SET deleted_at = 1000 WHERE id = ?", (keep,))
        self.sql("UPDATE notes SET deleted_at = 2000 WHERE id = ?", (back,))
        changed = mock.Mock()
        dialog = self._dialog(on_change=changed)
        self.assertEqual(len(_buttons(dialog, "Khôi phục")), 2)

        # rows are newest-deleted first, so the first button belongs to `back`
        _buttons(dialog, "Khôi phục")[0].invoke()
        dialog.update()

        self.assertIn(back, [n["id"] for n in self.store.list_notes()])
        self.assertEqual([t["id"] for t in self.store.list_trash()], [keep])
        changed.assert_called_once()
        self.assertEqual(len(_buttons(dialog, "Khôi phục")), 1)  # the list refreshed itself

    def test_purge_asks_first(self):
        nid = self.store.create_note("# delete forever")
        self.store.delete_note_by_id(nid)
        dialog = self._dialog()

        with mock.patch.object(self.trash_dialog.messagebox, "askyesno", return_value=False):
            _buttons(dialog, "Xoá vĩnh viễn")[0].invoke()
        self.assertEqual(len(self.store.list_trash()), 1)  # declined: still there

        with mock.patch.object(self.trash_dialog.messagebox, "askyesno", return_value=True):
            _buttons(dialog, "Xoá vĩnh viễn")[0].invoke()
        self.assertEqual(self.store.list_trash(), [])

    def test_empty_the_whole_trash(self):
        for i in range(3):
            self.store.delete_note_by_id(self.store.create_note(f"# n{i}"))
        dialog = self._dialog()
        with mock.patch.object(self.trash_dialog.messagebox, "askyesno", return_value=True):
            dialog._empty_btn.invoke()
        self.assertEqual(self.store.list_trash(), [])

    def test_days_left_counts_down_and_never_goes_negative(self):
        day = 86400
        self.assertEqual(self.trash_dialog._days_left(1000, now=1000), self.store.TRASH_RETENTION_DAYS)
        self.assertEqual(self.trash_dialog._days_left(1000, now=1000 + 59 * day + 1), 1)
        self.assertEqual(self.trash_dialog._days_left(1000, now=1000 + 999 * day), 0)


if __name__ == "__main__":
    unittest.main()
