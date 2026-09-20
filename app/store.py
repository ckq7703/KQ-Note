import json
import os
import shutil
import sqlite3
import time
import uuid

DEFAULT_CONTENT = (
    "# Nmap\n"
    "nmap -sV -sC -T4 <ip>          # scan version + script mac dinh\n"
    "nmap -p- -T4 <ip>              # scan toan bo 65535 port\n"
    "nmap -A <ip>                   # scan chi tiet (OS, version, traceroute)\n"
    "nmap -sn 192.168.1.0/24        # ping scan tim host song trong mang\n"
)


BACKUP_KEEP = 200


def get_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "NoteCheatsheet")
    os.makedirs(path, exist_ok=True)
    return path


def _atomic_write(path, data, mode):
    """Write to a temp file, fsync, then os.replace so a crash never leaves a truncated file."""
    tmp = f"{path}.{os.getpid()}.tmp"
    kwargs = {"encoding": "utf-8"} if "b" not in mode else {}
    try:
        with open(tmp, mode, **kwargs) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path, text):
    _atomic_write(path, text, "w")


def atomic_write_bytes(path, data):
    _atomic_write(path, data, "wb")


def get_backups_dir():
    path = os.path.join(get_data_dir(), "backups")
    os.makedirs(path, exist_ok=True)
    return path


def _prune_backups(directory):
    try:
        files = [os.path.join(directory, n) for n in os.listdir(directory) if n.endswith(".txt")]
        files.sort(key=os.path.getmtime, reverse=True)
        for old in files[BACKUP_KEEP:]:
            try:
                os.remove(old)
            except OSError:
                pass
    except OSError:
        pass


def backup_note_content(note_id, content, reason):
    """Keep a copy of note content that is about to be overwritten or deleted.

    Best-effort: returns the backup path, or None if there was nothing to keep
    or the copy failed. Never raises, so a backup problem can't block the caller.
    """
    if not (content or "").strip():
        return None
    try:
        directory = get_backups_dir()
        safe_id = "".join(c for c in str(note_id or "unknown") if c.isalnum() or c in "-_")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(directory, f"{safe_id}.{stamp}.{reason}.txt")
        n = 1
        while os.path.exists(path):
            path = os.path.join(directory, f"{safe_id}.{stamp}.{reason}.{n}.txt")
            n += 1
        atomic_write_text(path, content)
        _prune_backups(directory)
        return path
    except OSError:
        return None


def get_notes_path():
    return os.path.join(get_data_dir(), "notes.txt")


def get_images_dir():
    path = os.path.join(get_data_dir(), "images")
    os.makedirs(path, exist_ok=True)
    return path


def get_notes_store_dir():
    path = os.path.join(get_data_dir(), "notes_store")
    os.makedirs(path, exist_ok=True)
    return path


def get_index_path():
    return os.path.join(get_notes_store_dir(), "index.json")


def _extract_title_and_snippet(content):
    lines = (content or "").splitlines()
    title = "Ghi chú không tiêu đề"
    snippet = ""

    non_empty = [l.strip() for l in lines if l.strip()]
    if non_empty:
        first_line = non_empty[0]
        # Strip markdown headings or bullet prefixes
        cleaned = first_line.lstrip("#*->+ ").strip()
        if cleaned:
            title = cleaned[:40]

        # Snippet from subsequent lines or first line
        full_plain = " ".join(non_empty)
        snippet = full_plain[:100]

    return title, snippet


def _quarantine_corrupt_index(path):
    """Keep a copy of an unreadable index before anything rebuilds over it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    shutil.copy2(path, os.path.join(os.path.dirname(path), f"index.corrupt-{stamp}.json"))


def _load_index():
    path = get_index_path()
    if not os.path.exists(path):
        return None
    last_err = None
    for _ in range(3):  # transient locks (antivirus, indexer) shouldn't look like corruption
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _quarantine_corrupt_index(path)
            return None
        except OSError as e:
            last_err = e
            time.sleep(0.05)
            continue
        if not isinstance(data, dict):
            _quarantine_corrupt_index(path)
            return None
        return data
    raise last_err


def _save_index(data):
    atomic_write_text(get_index_path(), json.dumps(data, ensure_ascii=False, indent=2))


def _scan_note_files():
    """Rebuild index entries from note_*.txt files already on disk (newest first)."""
    notes_dir = get_notes_store_dir()
    found = []
    for name in os.listdir(notes_dir):
        if not (name.startswith("note_") and name.endswith(".txt")):
            continue
        path = os.path.join(notes_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            ts = int(os.path.getmtime(path))
        except (OSError, UnicodeDecodeError):
            continue
        title, snippet = _extract_title_and_snippet(content)
        found.append({
            "id": name[:-4], "title": title, "snippet": snippet,
            "updated_at": ts, "created_at": ts,
        })
    found.sort(key=lambda n: n["updated_at"], reverse=True)
    return found


def get_gemini_api_key():
    idx = _ensure_multi_notes_initialized()
    return idx.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")


def set_gemini_api_key(key):
    idx = _ensure_multi_notes_initialized()
    idx["gemini_api_key"] = key
    _save_index(idx)


def get_selected_gemini_model():
    idx = _ensure_multi_notes_initialized()
    return idx.get("selected_gemini_model") or "gemini-1.5-flash"


def set_selected_gemini_model(model_name):
    idx = _ensure_multi_notes_initialized()
    idx["selected_gemini_model"] = model_name
    _save_index(idx)


def _migrate_from_old_db():
    old_db = os.path.join(get_data_dir(), "notes.db")
    if not os.path.exists(old_db):
        return None
    try:
        conn = sqlite3.connect(old_db)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT content FROM notes ORDER BY id").fetchall()
        conn.close()
    except sqlite3.OperationalError:
        return None
    blocks = [r["content"] for r in rows if (r["content"] or "").strip()]
    if not blocks:
        return None
    return "\n\n".join(blocks) + "\n"


def _ensure_multi_notes_initialized():
    index_data = _load_index()
    if index_data is not None and "notes" in index_data:
        return index_data

    # Index missing/corrupt/without "notes": adopt note files already on disk
    # instead of re-initialising, which used to overwrite note_default.txt.
    recovered = _scan_note_files()
    if recovered:
        index_data = dict(index_data or {})
        index_data["notes"] = recovered
        index_data["active_note_id"] = recovered[0]["id"]
        _save_index(index_data)
        return index_data

    # Migration / First-time init
    legacy_txt = get_notes_path()
    initial_content = None
    if os.path.exists(legacy_txt):
        try:
            with open(legacy_txt, "r", encoding="utf-8") as f:
                initial_content = f.read()
        except Exception:
            pass

    if not initial_content:
        migrated = _migrate_from_old_db()
        initial_content = migrated if migrated is not None else DEFAULT_CONTENT

    note_id = "note_default"
    title, snippet = _extract_title_and_snippet(initial_content)
    now_ts = int(time.time())

    index_data = dict(index_data or {})  # keep unrelated keys (gemini settings)
    index_data["active_note_id"] = note_id
    index_data["notes"] = [
        {
            "id": note_id,
            "title": title,
            "snippet": snippet,
            "updated_at": now_ts,
            "created_at": now_ts,
        }
    ]

    note_file = os.path.join(get_notes_store_dir(), f"{note_id}.txt")
    if not os.path.exists(note_file):
        atomic_write_text(note_file, initial_content)

    _save_index(index_data)
    return index_data


def list_notes():
    idx = _ensure_multi_notes_initialized()
    return list(idx.get("notes", []))


def reorder_notes(ordered_ids):
    idx = _ensure_multi_notes_initialized()
    notes = idx.get("notes", [])
    notes_by_id = {n["id"]: n for n in notes}
    new_notes = []
    for nid in ordered_ids:
        if nid in notes_by_id:
            new_notes.append(notes_by_id.pop(nid))
    # Append any remaining
    new_notes.extend(notes_by_id.values())
    idx["notes"] = new_notes
    _save_index(idx)


def get_active_note_id():
    idx = _ensure_multi_notes_initialized()
    active_id = idx.get("active_note_id")
    notes = idx.get("notes", [])
    if notes:
        valid_ids = {n["id"] for n in notes}
        if active_id in valid_ids:
            return active_id
        return notes[0]["id"]
    return None


def set_active_note_id(note_id):
    idx = _ensure_multi_notes_initialized()
    idx["active_note_id"] = note_id
    _save_index(idx)


def load_note_by_id(note_id):
    _ensure_multi_notes_initialized()
    note_file = os.path.join(get_notes_store_dir(), f"{note_id}.txt")
    if os.path.exists(note_file):
        with open(note_file, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def save_note_by_id(note_id, content):
    idx = _ensure_multi_notes_initialized()
    note_file = os.path.join(get_notes_store_dir(), f"{note_id}.txt")
    atomic_write_text(note_file, content or "")

    title, snippet = _extract_title_and_snippet(content)
    now_ts = int(time.time())

    found = False
    for n in idx.get("notes", []):
        if n["id"] == note_id:
            n["title"] = title
            n["snippet"] = snippet
            n["updated_at"] = now_ts
            found = True
            break

    if not found:
        idx["notes"].append(
            {
                "id": note_id,
                "title": title,
                "snippet": snippet,
                "updated_at": now_ts,
                "created_at": now_ts,
            }
        )

    _save_index(idx)


def create_note(content="# Ghi chú mới\n\nNội dung ghi chú..."):
    idx = _ensure_multi_notes_initialized()
    note_id = f"note_{uuid.uuid4().hex[:8]}"
    title, snippet = _extract_title_and_snippet(content)
    now_ts = int(time.time())

    note_file = os.path.join(get_notes_store_dir(), f"{note_id}.txt")
    atomic_write_text(note_file, content or "")

    idx.get("notes", []).insert(
        0,
        {
            "id": note_id,
            "title": title,
            "snippet": snippet,
            "updated_at": now_ts,
            "created_at": now_ts,
        },
    )
    idx["active_note_id"] = note_id
    _save_index(idx)
    return note_id


def delete_note_by_id(note_id):
    idx = _ensure_multi_notes_initialized()
    notes = idx.get("notes", [])
    idx["notes"] = [n for n in notes if n["id"] != note_id]

    note_file = os.path.join(get_notes_store_dir(), f"{note_id}.txt")
    if os.path.exists(note_file):
        # No trash yet (Phase 2): keep a copy so a mistaken delete is recoverable.
        backup_note_content(note_id, load_note_by_id(note_id), "deleted")
        try:
            os.remove(note_file)
        except Exception:
            pass

    if not idx["notes"]:
        # If all notes were deleted, auto-create 1 fresh note
        new_id = create_note("# Ghi chú mới\n")
        return new_id

    if idx.get("active_note_id") == note_id:
        idx["active_note_id"] = idx["notes"][0]["id"]

    _save_index(idx)
    return idx["active_note_id"]


# ---- Compatibility Wrappers for Active Note ----
def load_content():
    active_id = get_active_note_id()
    if active_id:
        return load_note_by_id(active_id)
    return DEFAULT_CONTENT


def save_content(content):
    active_id = get_active_note_id()
    if not active_id:
        active_id = create_note(content)
    else:
        save_note_by_id(active_id, content)


def get_cloud_cache_path():
    """Local mirror of cloud account notes."""
    active_id = get_active_note_id() or "default"
    return os.path.join(get_data_dir(), f"notes.cloud.{active_id}.txt")


def load_cloud_cache():
    path = get_cloud_cache_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def save_cloud_cache(content):
    atomic_write_text(get_cloud_cache_path(), content or "")


def get_avatar_path():
    return os.path.join(get_data_dir(), "avatar.png")


def save_avatar(image_bytes):
    with open(get_avatar_path(), "wb") as f:
        f.write(image_bytes)


def load_avatar_bytes():
    path = get_avatar_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    return None


def clear_avatar():
    path = get_avatar_path()
    if os.path.exists(path):
        os.remove(path)
