"""Sync decisions, kept in the note database. No network here: engine.py does the I/O and
calls in with what the server said.

Model: each account note remembers the server's view of it (server_rev, server_deleted,
server_position) next to the local view (content/dirty, deleted_at, position). What still
has to be pushed is *derived* from the difference, so nothing is lost if the app dies
between two steps, and every step can simply be repeated.

Applying a server change never overwrites unpushed local edits: when both sides changed
the text, the local version is kept as a conflict copy (a new note), and the original
takes the server's version.
"""

import datetime
import time
import uuid

from app import store


# ------------------------------------------------------------------ small helpers

def _epoch(iso, default=None):
    """Server ISO timestamp -> epoch seconds (naive values are UTC)."""
    if iso:
        try:
            dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            pass
    return default if default is not None else int(time.time())


def _row(conn, note_id):
    return conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()


def get_note(note_id):
    with store._db() as conn:
        row = _row(conn, note_id)
    return dict(row) if row else None


def get_state(account_id):
    with store._db(write=True) as conn:
        conn.execute("INSERT OR IGNORE INTO sync_state (account_id) VALUES (?)", (account_id,))
        return dict(conn.execute("SELECT * FROM sync_state WHERE account_id = ?", (account_id,)).fetchone())


def _set_state(conn, account_id, **fields):
    conn.execute("INSERT OR IGNORE INTO sync_state (account_id) VALUES (?)", (account_id,))
    for key, value in fields.items():
        conn.execute(f"UPDATE sync_state SET {key} = ? WHERE account_id = ?", (value, account_id))


def set_state(account_id, **fields):
    with store._db(write=True) as conn:
        _set_state(conn, account_id, **fields)


def _adopt_position(row, remote):
    """(position, server_position) once the server's position is known: another device's
    move wins unless this device has a move of its own that hasn't been pushed."""
    pending_move = row["position"] != row["server_position"]
    position = row["position"] if pending_move else (remote["position"] or row["position"])
    return position, remote["position"]


# ------------------------------------------------------------------ applying what the server sent

def apply_page(account_id, changes, cursor=None):
    """Apply a batch of server notes in ONE transaction, together with the new cursor
    (when given), so a crash can't leave the cursor ahead of the data. Returns events:
    {"type": "changed"|"conflict"|"restored", "id": ..., ...}."""
    events = []
    with store._db(write=True) as conn:
        for remote in changes:
            _apply_one(conn, account_id, remote, events)
        if cursor is not None:
            _set_state(conn, account_id, cursor=cursor)
    return events


def _apply_one(conn, account_id, remote, events):
    row = _row(conn, remote["id"])
    if row is not None and row["account_id"] != account_id:
        return  # the same id exists under another scope on this device: not ours to touch

    if remote["purged"]:
        if row is not None:
            _remote_purged(conn, row, events)
        return
    if row is None:
        _insert_remote(conn, account_id, remote)
        events.append({"type": "changed", "id": remote["id"]})
        return
    if remote["rev"] < row["server_rev"]:
        return  # a replay of something older than what we already have
    if remote["rev"] == row["server_rev"]:
        position, server_position = _adopt_position(row, remote)
        if (position, server_position) != (row["position"], row["server_position"]):
            conn.execute("UPDATE notes SET position = ?, server_position = ? WHERE id = ?",
                         (position, server_position, row["id"]))
            events.append({"type": "changed", "id": row["id"]})
        return
    _apply_newer(conn, account_id, row, remote, events)


def _insert_remote(conn, account_id, remote):
    title, snippet = store._extract_title_and_snippet(remote["content"])
    deleted_at = _epoch(remote.get("deleted_at")) if remote["deleted"] else None
    conn.execute(
        "INSERT INTO notes (id, account_id, content, title, snippet, position, created_at, updated_at,"
        " deleted_at, server_rev, base_content, dirty, server_deleted, server_position)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (remote["id"], account_id, remote["content"], title, snippet, remote["position"],
         _epoch(remote.get("created_at")), _epoch(remote.get("updated_at")), deleted_at,
         remote["rev"], remote["content"], 1 if remote["deleted"] else 0, remote["position"]))


def _remote_purged(conn, row, events):
    """The server dropped this note for good."""
    if row["dirty"] and row["deleted_at"] is None:
        # Unpushed edits on a live note: the text must survive, so it becomes a new note.
        new_id = str(uuid.uuid4())
        conn.execute(
            "UPDATE notes SET id = ?, server_rev = 0, server_deleted = 0, server_position = NULL,"
            " base_content = NULL, dirty = 1 WHERE id = ?", (new_id, row["id"]))
        conn.execute("UPDATE kv SET value = ? WHERE key = 'active_note_id' AND value = ?", (new_id, row["id"]))
    else:
        store._purge_rows(conn, [row])  # leaves a copy in backups/
    events.append({"type": "changed", "id": row["id"]})


def _apply_newer(conn, account_id, row, remote, events):
    """The server has a newer revision than the one this device last saw."""
    local_dirty = bool(row["dirty"])
    local_deleted = row["deleted_at"] is not None
    trash_intent = local_deleted != bool(row["server_deleted"])  # this device wants to trash/restore
    remote_deleted = bool(remote["deleted"])
    same_content = remote["content"] == row["content"]

    content, dirty = remote["content"], 0
    deleted_at = _epoch(remote.get("deleted_at")) if remote_deleted else None

    if local_dirty:
        # What did the server change since the text this device last agreed on (base_content)?
        base = row["base_content"] if row["base_content"] is not None else row["content"]
        remote_edited = remote["content"] != base
        if same_content:
            # Both sides ended up with identical text: nothing to keep apart. Follow the server's
            # trash state, except for a delete we just made ourselves.
            if not remote_deleted and local_deleted and trash_intent:
                deleted_at = row["deleted_at"]
        elif not remote_edited:
            # The revision moved (trash/restore/reorder elsewhere) but the text didn't: our edit stands.
            content, dirty = row["content"], 1
            if remote_deleted:
                deleted_at = row["deleted_at"] if local_deleted else None
                if not local_deleted:
                    events.append({"type": "restored", "id": row["id"]})  # edited here, deleted there: the edit wins
            else:
                deleted_at = row["deleted_at"] if (local_deleted and trash_intent) else None
        else:
            # Both sides wrote different text (trashed or not): never pick one, keep ours as a new note.
            copy_id = store.create_conflict_copy(conn, account_id, row["title"], row["content"])
            events.append({"type": "conflict", "id": row["id"], "copy_id": copy_id, "title": row["title"]})
    elif trash_intent:
        if local_deleted and remote_deleted:
            deleted_at = row["deleted_at"]  # both sides want it in the trash
        elif local_deleted and same_content:
            deleted_at = row["deleted_at"]  # revision moved but nothing was edited: keep the delete
        elif local_deleted:
            events.append({"type": "restored", "id": row["id"]})  # edited elsewhere: edit wins over our delete
        elif remote_deleted:
            deleted_at = None  # we want it back; the restore will be pushed again on the new revision

    position, server_position = _adopt_position(row, remote)
    title, snippet = store._extract_title_and_snippet(content)
    updated_at = _epoch(remote.get("updated_at")) if content == remote["content"] else row["updated_at"]
    conn.execute(
        "UPDATE notes SET content = ?, title = ?, snippet = ?, updated_at = ?, deleted_at = ?, dirty = ?,"
        " server_rev = ?, server_deleted = ?, base_content = ?, position = ?, server_position = ? WHERE id = ?",
        (content, title, snippet, updated_at, deleted_at, dirty, remote["rev"], 1 if remote_deleted else 0,
         remote["content"], position, server_position, row["id"]))
    events.append({"type": "changed", "id": row["id"]})


def finish_full_pass(account_id, seen_ids, cursor):
    """After reading the whole change feed from 0: local notes the server no longer has.

    They are never dropped silently: a live note goes back to "never synced" so it is
    re-uploaded (the server may simply have been restored from an older backup); one
    already in the trash is purged locally."""
    events = []
    with store._db(write=True) as conn:
        rows = conn.execute("SELECT * FROM notes WHERE account_id = ? AND server_rev > 0", (account_id,)).fetchall()
        for row in rows:
            if row["id"] in seen_ids:
                continue
            if row["deleted_at"] is not None:
                store._purge_rows(conn, [row])
                events.append({"type": "changed", "id": row["id"]})
            else:
                conn.execute(
                    "UPDATE notes SET server_rev = 0, server_deleted = 0, server_position = NULL,"
                    " base_content = NULL, dirty = 1 WHERE id = ?", (row["id"],))
        _set_state(conn, account_id, cursor=cursor)
    return events


def import_legacy_slot(account_id, content):
    """Bring in the single-note cloud blob of the pre-multi-note versions, unless the account
    already holds that text (the server copied it into a note when it migrated). Marks the
    import as done either way. Returns the new note id or None."""
    with store._db(write=True) as conn:
        created = None
        if content and content.strip():
            existing = {r[0].strip() for r in conn.execute("SELECT content FROM notes WHERE account_id = ?", (account_id,))}
            if content.strip() not in existing:
                created = store._insert_note(conn, content, account_id=account_id)
        _set_state(conn, account_id, legacy_imported=1)
    return created


# ------------------------------------------------------------------ what to push

def next_op(row):
    """The next server call this note needs, or None when the server is up to date.

    ("put", restore)  create/update content. restore=True is used while the server has the
                      note in its trash: the note is taken out just for the write and the
                      next call trashes it again, so text typed here always reaches the server.
    ("trash",) / ("restore",) / ("move",)
    """
    local_deleted = row["deleted_at"] is not None
    server_deleted = bool(row["server_deleted"])
    if row["server_rev"] == 0:
        if _is_starter_text(row["content"]):
            return None  # untouched starter text (or blank): not worth a server copy until the user writes something
        return ("put", False)  # anything else, even if already trashed here, so the trash matches on every device
    if row["dirty"]:
        return ("put", server_deleted)
    if local_deleted and not server_deleted:
        return ("trash",)
    if not local_deleted and server_deleted:
        return ("restore",)
    if not local_deleted and row["position"] != row["server_position"]:
        return ("move",)
    return None


def _is_starter_text(content):
    starters = {c.strip() for c in store.PLACEHOLDER_CONTENTS} | {store.DEFAULT_CONTENT.strip()}
    return not (content or "").strip() or content.strip() in starters


def push_candidates(account_id):
    """Ids of the account's notes that need at least one server call, in list order."""
    with store._db() as conn:
        rows = conn.execute(
            "SELECT * FROM notes WHERE account_id = ? AND (dirty = 1 OR server_rev = 0"
            " OR (deleted_at IS NOT NULL) != (server_deleted != 0) OR position IS NOT server_position)"
            " ORDER BY position, id", (account_id,)).fetchall()
    return [r["id"] for r in rows if next_op(r) is not None]


def record_server_note(note_id, server_note, pushed_content=None):
    """Remember the server's answer to one of our calls. When `pushed_content` is given, the
    note stays dirty if the user typed more while the request was in flight."""
    with store._db(write=True) as conn:
        row = _row(conn, note_id)
        if row is None or server_note["rev"] < row["server_rev"]:
            return
        dirty = row["dirty"]
        if pushed_content is not None:
            dirty = 0 if row["content"] == pushed_content else 1
        conn.execute(
            "UPDATE notes SET server_rev = ?, server_deleted = ?, server_position = ?, base_content = ?, dirty = ?"
            " WHERE id = ?",
            (server_note["rev"], 1 if server_note["deleted"] else 0, server_note["position"],
             server_note["content"], dirty, note_id))


def reset_unsynced(note_id):
    """The server has no such note any more: upload it again as a new one."""
    with store._db(write=True) as conn:
        conn.execute(
            "UPDATE notes SET server_rev = 0, server_deleted = 0, server_position = NULL, base_content = NULL,"
            " dirty = 1 WHERE id = ?", (note_id,))
