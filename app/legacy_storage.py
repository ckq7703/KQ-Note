"""Read the storage layouts used before the SQLite store, without modifying them.

Layouts, newest first:
  notes_store/index.json + notes_store/note_*.txt   (multi-note, files)
  notes.txt                                          (single note)
  notes.db                                           (SQLite, one row per block)

Nothing here writes to or deletes the old files (a corrupt index.json is only
copied aside), so they remain as a backup after the import.
"""

import json
import os
import shutil
import sqlite3
import time

LEGACY_DEFAULT_ID = "note_default"


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _load_index(store_dir):
    """The parsed index.json, or None if missing/corrupt. A corrupt file is copied to
    index.corrupt-<ts>.json first. Transient I/O errors propagate: importing a
    partial view of the user's notes is worse than failing loudly."""
    path = os.path.join(store_dir, "index.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, dict):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(path, os.path.join(store_dir, f"index.corrupt-{stamp}.json"))
        return None
    return data


def _scan_note_files(store_dir):
    """legacy_id -> (content, mtime) for every note_*.txt in the directory."""
    found = {}
    if not os.path.isdir(store_dir):
        return found
    for name in os.listdir(store_dir):
        if name.startswith("note_") and name.endswith(".txt"):
            path = os.path.join(store_dir, name)
            found[name[:-4]] = (_read_text(path), int(os.path.getmtime(path)))
    return found


def _legacy_single_note(data_dir):
    """Content of the pre-multi-note storage (notes.txt, else the old notes.db), or None."""
    txt = os.path.join(data_dir, "notes.txt")
    if os.path.exists(txt):
        content = _read_text(txt)
        if content.strip():
            return content
    old_db = os.path.join(data_dir, "notes.db")
    if os.path.exists(old_db):
        try:
            conn = sqlite3.connect(old_db)
            try:
                rows = conn.execute("SELECT content FROM notes ORDER BY id").fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return None
        blocks = [r[0] for r in rows if (r[0] or "").strip()]
        if blocks:
            return "\n\n".join(blocks) + "\n"
    return None


def read_legacy_data(data_dir):
    """Everything worth carrying into the SQLite store.

    Returns {"notes": [...top-to-bottom...], "active_legacy_id", "gemini_api_key",
    "selected_gemini_model"}; each note is {"legacy_id", "content", "created_at",
    "updated_at"}. "notes" is empty for a genuinely fresh install.
    """
    store_dir = os.path.join(data_dir, "notes_store")
    now = int(time.time())
    index = _load_index(store_dir)
    files = _scan_note_files(store_dir)

    notes, seen = [], set()
    entries = index.get("notes") if index else None
    for entry in entries if isinstance(entries, list) else []:
        legacy_id = entry.get("id") if isinstance(entry, dict) else None
        if not legacy_id or legacy_id in seen:
            continue
        content, mtime = files.get(legacy_id, ("", now))
        # A note the index lists but whose file is gone stays as an empty note so the
        # user's list is not silently shortened.
        notes.append({
            "legacy_id": legacy_id,
            "content": content,
            "created_at": int(entry.get("created_at") or mtime),
            "updated_at": int(entry.get("updated_at") or mtime),
        })
        seen.add(legacy_id)

    # Note files the index doesn't know about (index lost/corrupt/behind), newest first.
    orphans = [(lid, c, m) for lid, (c, m) in files.items() if lid not in seen and c.strip()]
    for legacy_id, content, mtime in sorted(orphans, key=lambda o: o[2], reverse=True):
        notes.append({"legacy_id": legacy_id, "content": content, "created_at": mtime, "updated_at": mtime})

    if not notes:
        content = _legacy_single_note(data_dir)
        if content is not None:
            notes.append({"legacy_id": LEGACY_DEFAULT_ID, "content": content, "created_at": now, "updated_at": now})

    index = index or {}
    return {
        "notes": notes,
        "active_legacy_id": index.get("active_note_id"),
        "gemini_api_key": index.get("gemini_api_key"),
        "selected_gemini_model": index.get("selected_gemini_model"),
    }
