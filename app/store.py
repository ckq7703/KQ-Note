import os
import sqlite3

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


def load_content():
    path = get_notes_path()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    migrated = _migrate_from_old_db()
    content = migrated if migrated is not None else DEFAULT_CONTENT
    save_content(content)
    return content


def save_content(content):
    path = get_notes_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")


def get_cloud_cache_path():
    """Local mirror of whichever cloud account is currently logged in, kept
    entirely separate from the local-only note so logging out/in never mixes
    the two: logged out shows notes.txt, logged in shows this file."""
    return os.path.join(get_data_dir(), "notes.cloud.txt")


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
