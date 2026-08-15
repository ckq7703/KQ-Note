"""Local sync bookkeeping: which server version/hash this device last synced to.

Kept separate from config.json (user preferences) and notes.txt (the actual
content) since it's purely sync-engine housekeeping.
"""

import json
import os
import uuid

from app.store import get_data_dir

_DEFAULT_STATE = {
    "device_id": None,
    "last_synced_version": None,
    "last_synced_hash": None,
}


def _state_path():
    return os.path.join(get_data_dir(), "sync_state.json")


def load_state():
    path = _state_path()
    state = dict(_DEFAULT_STATE)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state.update(json.load(f) or {})
        except (json.JSONDecodeError, OSError):
            pass
    if not state.get("device_id"):
        state["device_id"] = uuid.uuid4().hex
        _save_state(state)
    return state


def _save_state(state):
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump(state, f)


def update_after_sync(version, content_hash):
    state = load_state()
    state["last_synced_version"] = version
    state["last_synced_hash"] = content_hash
    _save_state(state)


def clear():
    """Called on logout: forget sync progress, but keep the device_id stable."""
    state = load_state()
    state["last_synced_version"] = None
    state["last_synced_hash"] = None
    _save_state(state)


def write_conflict_backup(remote_content):
    """Stash a competing remote version that lost to local-first conflict resolution."""
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(get_data_dir(), f"notes.conflict-{timestamp}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(remote_content or "")
    return path
