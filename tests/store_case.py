import importlib
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class StoreCase(unittest.TestCase):
    """Runs against a throw-away data directory; `restart()` simulates relaunching the app."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_appdata = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self._tmp.name
        self.data_dir = os.path.join(self._tmp.name, "NoteCheatsheet")
        os.makedirs(self.data_dir, exist_ok=True)
        self.restart()

    def tearDown(self):
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata
        self._tmp.cleanup()

    def restart(self):
        from app import store
        self.store = importlib.reload(store)
        return self.store

    # -- direct database access, bypassing the store API
    def sql(self, query, params=()):
        conn = sqlite3.connect(self.store.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    # -- pre-SQLite layouts
    def write_legacy(self, notes, active=None, index_extra=None, write_index=True):
        """notes: [(legacy_id, content)] top to bottom. Returns the notes_store dir."""
        store_dir = os.path.join(self.data_dir, "notes_store")
        os.makedirs(store_dir, exist_ok=True)
        entries = []
        for i, (legacy_id, content) in enumerate(notes):
            with open(os.path.join(store_dir, f"{legacy_id}.txt"), "w", encoding="utf-8") as f:
                f.write(content)
            entries.append({"id": legacy_id, "title": "t", "snippet": "s",
                            "updated_at": 1_700_000_000 + i, "created_at": 1_690_000_000 + i})
        if write_index:
            index = {"active_note_id": active, "notes": entries}
            index.update(index_extra or {})
            with open(os.path.join(store_dir, "index.json"), "w", encoding="utf-8") as f:
                json.dump(index, f)
        return store_dir

    def snapshot_dir(self, path):
        """{relative path: bytes} for every file under path."""
        out = {}
        for root, _dirs, files in os.walk(path):
            for name in files:
                full = os.path.join(root, name)
                with open(full, "rb") as f:
                    out[os.path.relpath(full, path)] = f.read()
        return out
