import importlib
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class StoreSafetyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self._tmp.name
        from app import store
        self.store = importlib.reload(store)

    def tearDown(self):
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        self._tmp.cleanup()

    def _backups(self):
        return sorted(os.listdir(self.store.get_backups_dir()))

    def test_atomic_write_replaces_and_leaves_no_temp_file(self):
        path = os.path.join(self._tmp.name, "f.txt")
        self.store.atomic_write_text(path, "one")
        self.store.atomic_write_text(path, "two")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "two")
        self.assertEqual([n for n in os.listdir(self._tmp.name) if n.endswith(".tmp")], [])

    def test_corrupt_index_is_recovered_without_losing_notes(self):
        a = self.store.create_note("# Alpha\nbody a")
        b = self.store.create_note("# Beta\nbody b")
        self.store.save_note_by_id("note_default", "# Default\nkeep me")
        with open(self.store.get_index_path(), "w", encoding="utf-8") as f:
            f.write("{ this is not json")

        ids = {n["id"] for n in self.store.list_notes()}

        self.assertEqual(ids, {a, b, "note_default"})
        self.assertEqual(self.store.load_note_by_id("note_default"), "# Default\nkeep me")
        self.assertEqual(self.store.load_note_by_id(a), "# Alpha\nbody a")
        quarantined = [n for n in os.listdir(self.store.get_notes_store_dir())
                       if n.startswith("index.corrupt-")]
        self.assertEqual(len(quarantined), 1)

    def test_recovery_preserves_extra_index_keys_when_notes_key_missing(self):
        self.store.set_gemini_api_key("secret-key")
        nid = self.store.create_note("# Keep\nx")
        with open(self.store.get_index_path(), "w", encoding="utf-8") as f:
            json.dump({"gemini_api_key": "secret-key"}, f)

        self.assertIn(nid, {n["id"] for n in self.store.list_notes()})
        self.assertEqual(self.store.get_gemini_api_key(), "secret-key")

    def test_delete_keeps_a_backup_copy(self):
        nid = self.store.create_note("# Precious\nimportant text")
        self.store.delete_note_by_id(nid)

        backups = [n for n in self._backups() if n.startswith(nid) and ".deleted" in n]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.store.get_backups_dir(), backups[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "# Precious\nimportant text")

    def test_backup_skips_empty_and_never_clobbers_same_second(self):
        self.assertIsNone(self.store.backup_note_content("n1", "   ", "x"))
        p1 = self.store.backup_note_content("n1", "first", "x")
        p2 = self.store.backup_note_content("n1", "second", "x")
        self.assertNotEqual(p1, p2)
        with open(p1, encoding="utf-8") as f:
            self.assertEqual(f.read(), "first")

    def test_backup_prunes_to_limit(self):
        self.store.BACKUP_KEEP = 3
        for i in range(6):
            self.store.backup_note_content(f"n{i}", f"content {i}", "x")
        self.assertEqual(len(self._backups()), 3)


if __name__ == "__main__":
    unittest.main()
