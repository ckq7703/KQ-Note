import json
import os
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


def get_data_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "NoteCheatsheet")
    os.makedirs(path, exist_ok=True)
    return path


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


def _load_index():
    path = get_index_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_index(data):
    path = get_index_path()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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

    index_data = {
        "active_note_id": note_id,
        "notes": [
            {
                "id": note_id,
                "title": title,
                "snippet": snippet,
                "updated_at": now_ts,
                "created_at": now_ts,
            }
        ],
    }

    note_file = os.path.join(get_notes_store_dir(), f"{note_id}.txt")
    with open(note_file, "w", encoding="utf-8") as f:
        f.write(initial_content)

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
    with open(note_file, "w", encoding="utf-8") as f:
        f.write(content or "")

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
    with open(note_file, "w", encoding="utf-8") as f:
        f.write(content or "")

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
    with open(get_cloud_cache_path(), "w", encoding="utf-8") as f:
        f.write(content or "")


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
