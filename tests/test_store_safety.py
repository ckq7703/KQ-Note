import os
import unittest

from tests.store_case import StoreCase


class FileSafetyTest(StoreCase):
    """Atomic writes and best-effort disk backups (the parts of the store that still use files)."""

    def _backups(self):
        return sorted(os.listdir(self.store.get_backups_dir()))

    def test_atomic_write_replaces_and_leaves_no_temp_file(self):
        path = os.path.join(self.data_dir, "f.txt")
        self.store.atomic_write_text(path, "one")
        self.store.atomic_write_text(path, "two")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "two")
        self.assertEqual([n for n in os.listdir(self.data_dir) if n.endswith(".tmp")], [])

    def test_atomic_write_bytes(self):
        path = os.path.join(self.data_dir, "b.bin")
        self.store.atomic_write_bytes(path, b"\x00\x01")
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"\x00\x01")

    def test_a_failed_atomic_write_keeps_the_old_file(self):
        path = os.path.join(self.data_dir, "f.txt")
        self.store.atomic_write_text(path, "good")
        with self.assertRaises(TypeError):
            self.store.atomic_write_text(path, 123)  # not writable as text
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "good")
        self.assertEqual([n for n in os.listdir(self.data_dir) if n.endswith(".tmp")], [])

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
