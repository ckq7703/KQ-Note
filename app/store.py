"""Local persistence: notes live in one SQLite database (WAL, synchronous=FULL).

The first run against a data directory imports the older file layouts (see
app.legacy_storage) in a single transaction and leaves those files untouched as a
backup. Public note functions keep the names the UI already used; everything about
trash, account scoping and reordering is new here.
"""

import contextlib
import os
import sqlite3
import threading
import time
import uuid

from app import fracindex, legacy_storage

DEFAULT_CONTENT = (
    "# Nmap\n"
    "nmap -sV -sC -T4 <ip>          # scan version + script mac dinh\n"
    "nmap -p- -T4 <ip>              # scan toan bo 65535 port\n"
    "nmap -A <ip>                   # scan chi tiet (OS, version, traceroute)\n"
    "nmap -sn 192.168.1.0/24        # ping scan tim host song trong mang\n"
)

BACKUP_KEEP = 200
TRASH_RETENTION_DAYS = 60  # same as OneNote's recycle bin, and the server's purge window
MAX_KEY_LEN = 24  # renumber a scope's positions before keys outgrow this (server cap is 64)
SCHEMA_VERSION = 1
DB_FILENAME = "kqnote.sqlite3"  # not notes.db: that name belonged to an even older layout


# ------------------------------------------------------------------ files

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
    """Keep a copy of note content that is about to be overwritten or destroyed.

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


def get_images_dir():
    path = os.path.join(get_data_dir(), "images")
    os.makedirs(path, exist_ok=True)
    return path


def get_db_path():
    return os.path.join(get_data_dir(), DB_FILENAME)


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


# ------------------------------------------------------------------ database

_READY = set()
_READY_LOCK = threading.Lock()
_scope = None  # account id whose notes are visible; None = the local-only notes


def _open(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit: we BEGIN explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


@contextlib.contextmanager
def _db(write=False):
    """A connection; with write=True everything inside is one atomic transaction."""
    _ensure_ready()
    conn = _open(get_db_path())
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        if write:
            conn.execute("COMMIT")
    except BaseException:
        if write and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def initialize():
    """Open (and on first run, create/import) the database. Raises if that fails."""
    _ensure_ready()


def _ensure_ready():
    path = get_db_path()
    if path in _READY:
        return
    with _READY_LOCK:
        if path in _READY:
            return
        _initialize(path)
        _READY.add(path)


def _initialize(path):
    conn = _open(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
                    _create_schema(conn)
                    _import_initial_data(conn)
                    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        conn.execute("BEGIN IMMEDIATE")
        try:
            _purge_expired(conn, int(time.time()))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _create_schema(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notes (
            id          TEXT PRIMARY KEY,
            account_id  TEXT,                       -- NULL = local-only, else the owning account
            content     TEXT NOT NULL DEFAULT '',
            title       TEXT NOT NULL DEFAULT '',
            snippet     TEXT NOT NULL DEFAULT '',
            position    TEXT NOT NULL DEFAULT '',   -- fractional index, see app.fracindex
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL,
            deleted_at  INTEGER,                    -- set while the note is in the trash
            legacy_id   TEXT,                       -- id in the pre-SQLite layout, for traceability
            server_rev  INTEGER NOT NULL DEFAULT 0, -- sync: last server revision seen (0 = never synced)
            base_content TEXT,                      -- sync: content as of server_rev
            dirty       INTEGER NOT NULL DEFAULT 0  -- sync: content changed since the last push
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_scope ON notes(account_id, deleted_at, position)")
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    # Which local-only notes were already copied into which account, so logging in
    # again never duplicates them.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS adoptions (
            local_id   TEXT NOT NULL,
            account_id TEXT NOT NULL,
            PRIMARY KEY (local_id, account_id)
        )"""
    )


def _import_initial_data(conn):
    legacy = legacy_storage.read_legacy_data(get_data_dir())
    records = legacy["notes"]
    if not records:
        now = int(time.time())
        records = [{"legacy_id": None, "content": DEFAULT_CONTENT, "created_at": now, "updated_at": now}]

    keys = fracindex.n_keys_between(None, None, len(records))
    id_by_legacy, expected_chars = {}, 0
    for record, position in zip(records, keys):
        note_id = str(uuid.uuid4())
        title, snippet = _extract_title_and_snippet(record["content"])
        conn.execute(
            "INSERT INTO notes (id, account_id, content, title, snippet, position, created_at, updated_at, legacy_id)"
            " VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (note_id, record["content"], title, snippet, position,
             record["created_at"], record["updated_at"], record["legacy_id"]),
        )
        if record["legacy_id"]:
            id_by_legacy[record["legacy_id"]] = note_id
        expected_chars += len(record["content"])

    count, chars = conn.execute("SELECT count(*), coalesce(sum(length(content)), 0) FROM notes").fetchone()
    if count != len(records) or chars != expected_chars:
        raise RuntimeError(f"Note import verification failed: {count}/{len(records)} notes, {chars}/{expected_chars} chars")

    first_id = conn.execute("SELECT id FROM notes ORDER BY position LIMIT 1").fetchone()[0]
    _kv_set(conn, "active_note_id", id_by_legacy.get(legacy["active_legacy_id"], first_id))
    if legacy["gemini_api_key"]:
        _kv_set(conn, "gemini_api_key", legacy["gemini_api_key"])
    if legacy["selected_gemini_model"]:
        _kv_set(conn, "selected_gemini_model", legacy["selected_gemini_model"])


def _kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _kv_set(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, value))


# ------------------------------------------------------------------ positions

def _renumber(conn):
    """Give every live note in the scope a fresh, short, evenly spread position (keeping order)."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM notes WHERE account_id IS ? AND deleted_at IS NULL ORDER BY position, id", (_scope,))]
    keys = fracindex.n_keys_between(None, None, len(ids))
    conn.executemany("UPDATE notes SET position = ? WHERE id = ?", zip(keys, ids))


def _safe_key_between(a, b):
    """key_between, or None when it can't be used (bad foreign key, or too long)."""
    try:
        key = fracindex.key_between(a, b)
    except ValueError:
        return None
    return key if len(key) <= MAX_KEY_LEN else None


def _put_on_top(conn, note_id):
    """Position an existing row above every other live note in the scope."""
    first = conn.execute(
        "SELECT position FROM notes WHERE account_id IS ? AND deleted_at IS NULL AND id != ?"
        " ORDER BY position, id LIMIT 1", (_scope, note_id)).fetchone()
    key = _safe_key_between(None, first[0] if first else None)
    if key is None:
        conn.execute("UPDATE notes SET position = '' WHERE id = ?", (note_id,))  # '' sorts first
        _renumber(conn)
    else:
        conn.execute("UPDATE notes SET position = ? WHERE id = ?", (key, note_id))


def _insert_note(conn, content, note_id=None):
    now = int(time.time())
    note_id = note_id or str(uuid.uuid4())
    title, snippet = _extract_title_and_snippet(content)
    conn.execute(
        "INSERT INTO notes (id, account_id, content, title, snippet, position, created_at, updated_at, dirty)"
        " VALUES (?, ?, ?, ?, ?, '', ?, ?, ?)",
        (note_id, _scope, content, title, snippet, now, now, 1 if _scope else 0),
    )
    _put_on_top(conn, note_id)
    return note_id


def _single_moved(old, new):
    """The one id whose move turns `old` into `new`, or None if it isn't a single move."""
    if len(old) != len(new) or set(old) != set(new):
        return None
    i = next(i for i, (a, b) in enumerate(zip(old, new)) if a != b)
    for candidate in (old[i], new[i]):
        if [x for x in old if x != candidate] == [x for x in new if x != candidate]:
            return candidate
    return None


# ------------------------------------------------------------------ account scope

def set_scope(account_id):
    """Choose whose notes the note functions operate on (None = local-only notes)."""
    global _scope
    _scope = account_id or None


def get_scope():
    return _scope


def copy_local_notes_to_account(account_id):
    """Copy the local-only notes into `account_id` as new notes (marked dirty so they
    get uploaded). The local originals stay, so logging out still shows them, and each
    is copied at most once per account. Returns how many notes were copied."""
    copied = 0
    with _db(write=True) as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE account_id IS NULL AND deleted_at IS NULL"
            " AND trim(content, ' ' || char(9) || char(10) || char(13)) != ''"
            " AND id NOT IN (SELECT local_id FROM adoptions WHERE account_id = ?)"
            " ORDER BY position, id", (account_id,)).fetchall()
        for row in rows:
            conn.execute(
                "INSERT INTO notes (id, account_id, content, title, snippet, position, created_at, updated_at,"
                " legacy_id, server_rev, dirty) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1)",
                (str(uuid.uuid4()), account_id, row["content"], row["title"], row["snippet"],
                 row["position"], row["created_at"], row["updated_at"], row["legacy_id"]))
            conn.execute("INSERT INTO adoptions (local_id, account_id) VALUES (?, ?)", (row["id"], account_id))
            copied += 1
    return copied


# ------------------------------------------------------------------ notes

def list_notes():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, title, snippet, updated_at, created_at FROM notes"
            " WHERE account_id IS ? AND deleted_at IS NULL ORDER BY position, id", (_scope,)).fetchall()
    return [dict(r) for r in rows]


def reorder_notes(ordered_ids):
    with _db(write=True) as conn:
        rows = conn.execute(
            "SELECT id, position FROM notes WHERE account_id IS ? AND deleted_at IS NULL ORDER BY position, id",
            (_scope,)).fetchall()
        old = [r["id"] for r in rows]
        position = {r["id"]: r["position"] for r in rows}
        known = set(old)
        new, seen = [], set()
        for note_id in ordered_ids:
            if note_id in known and note_id not in seen:
                new.append(note_id)
                seen.add(note_id)
        new.extend(i for i in old if i not in seen)  # anything not mentioned keeps its relative order, at the end
        if new == old:
            return

        moved = _single_moved(old, new)
        if moved is not None:
            at = new.index(moved)
            key = _safe_key_between(position[new[at - 1]] if at > 0 else None,
                                    position[new[at + 1]] if at + 1 < len(new) else None)
            if key is not None:
                conn.execute("UPDATE notes SET position = ? WHERE id = ?", (key, moved))
                return
        keys = fracindex.n_keys_between(None, None, len(new))
        conn.executemany("UPDATE notes SET position = ? WHERE id = ?", zip(keys, new))


def get_active_note_id():
    with _db() as conn:
        active = _kv_get(conn, "active_note_id")
        live = conn.execute(
            "SELECT id FROM notes WHERE account_id IS ? AND deleted_at IS NULL ORDER BY position, id",
            (_scope,)).fetchall()
    ids = [r[0] for r in live]
    if active in ids:
        return active
    return ids[0] if ids else None


def set_active_note_id(note_id):
    with _db(write=True) as conn:
        _kv_set(conn, "active_note_id", note_id)


def load_note_by_id(note_id):
    with _db() as conn:
        row = conn.execute("SELECT content FROM notes WHERE id = ?", (note_id,)).fetchone()
    return row[0] if row else ""


def save_note_by_id(note_id, content):
    content = content or ""
    title, snippet = _extract_title_and_snippet(content)
    with _db(write=True) as conn:
        row = conn.execute("SELECT content FROM notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            _insert_note(conn, content, note_id)
        elif row[0] != content:  # unchanged content must not look like an edit
            conn.execute(
                "UPDATE notes SET content = ?, title = ?, snippet = ?, updated_at = ?,"
                " dirty = CASE WHEN account_id IS NULL THEN dirty ELSE 1 END WHERE id = ?",
                (content, title, snippet, int(time.time()), note_id))


def create_note(content="# Ghi chú mới\n\nNội dung ghi chú..."):
    with _db(write=True) as conn:
        note_id = _insert_note(conn, content or "")
        _kv_set(conn, "active_note_id", note_id)
    return note_id


def delete_note_by_id(note_id):
    """Move a note to the trash. Returns the id of the note that should be active afterwards."""
    with _db(write=True) as conn:
        conn.execute("UPDATE notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                     (int(time.time()), note_id))
        live = [r[0] for r in conn.execute(
            "SELECT id FROM notes WHERE account_id IS ? AND deleted_at IS NULL ORDER BY position, id",
            (_scope,))]
        if not live:
            active = _insert_note(conn, "# Ghi chú mới\n")  # never leave the user with no note
        else:
            active = _kv_get(conn, "active_note_id")
            if active not in live:
                active = live[0]
        _kv_set(conn, "active_note_id", active)
    return active


def load_content():
    active_id = get_active_note_id()
    if active_id:
        return load_note_by_id(active_id)
    return DEFAULT_CONTENT


def save_content(content):
    active_id = get_active_note_id()
    if not active_id:
        create_note(content)
    else:
        save_note_by_id(active_id, content)


# ------------------------------------------------------------------ trash

def list_trash():
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, title, snippet, deleted_at, updated_at FROM notes"
            " WHERE account_id IS ? AND deleted_at IS NOT NULL ORDER BY deleted_at DESC, id",
            (_scope,)).fetchall()
    return [dict(r) for r in rows]


def restore_note(note_id):
    """Take a note out of the trash and put it at the top of the list. False if it isn't trashed."""
    with _db(write=True) as conn:
        cur = conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL", (note_id,))
        if cur.rowcount != 1:
            return False
        _put_on_top(conn, note_id)
    return True


def _purge_rows(conn, rows):
    for row in rows:
        backup_note_content(row["id"], row["content"], "purged")  # last resort copy on disk
        conn.execute("DELETE FROM notes WHERE id = ?", (row["id"],))
        conn.execute("DELETE FROM adoptions WHERE local_id = ?", (row["id"],))
    return len(rows)


def purge_note(note_id):
    """Delete a trashed note for good. False if it isn't in the trash."""
    with _db(write=True) as conn:
        rows = conn.execute("SELECT id, content FROM notes WHERE id = ? AND deleted_at IS NOT NULL",
                            (note_id,)).fetchall()
        return _purge_rows(conn, rows) == 1


def empty_trash():
    with _db(write=True) as conn:
        rows = conn.execute("SELECT id, content FROM notes WHERE account_id IS ? AND deleted_at IS NOT NULL",
                            (_scope,)).fetchall()
        return _purge_rows(conn, rows)


def _purge_expired(conn, now):
    cutoff = now - TRASH_RETENTION_DAYS * 86400
    rows = conn.execute("SELECT id, content FROM notes WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                        (cutoff,)).fetchall()
    return _purge_rows(conn, rows)


def purge_expired_trash(now=None):
    """Purge notes trashed more than TRASH_RETENTION_DAYS ago (all scopes). Returns how many."""
    with _db(write=True) as conn:
        return _purge_expired(conn, int(now if now is not None else time.time()))


# ------------------------------------------------------------------ settings (Gemini)

def get_gemini_api_key():
    with _db() as conn:
        return _kv_get(conn, "gemini_api_key") or os.environ.get("GEMINI_API_KEY", "")


def set_gemini_api_key(key):
    with _db(write=True) as conn:
        _kv_set(conn, "gemini_api_key", key)


def get_selected_gemini_model():
    with _db() as conn:
        return _kv_get(conn, "selected_gemini_model") or "gemini-1.5-flash"


def set_selected_gemini_model(model_name):
    with _db(write=True) as conn:
        _kv_set(conn, "selected_gemini_model", model_name)


# ------------------------------------------------------------------ cloud cache / avatar (files)

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
