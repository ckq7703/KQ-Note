"""Per-installation sync identity.

The device id is sent with every write so a note's `updated_by_device` says which
installation changed it. Everything else about sync progress now lives in the note
database (see app.sync.repo).
"""

import json
import os
import uuid

from app.store import atomic_write_text, get_data_dir


def _state_path():
    return os.path.join(get_data_dir(), "sync_state.json")


def get_device_id():
    path = _state_path()
    state = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            state = {}
    if not state.get("device_id"):
        state["device_id"] = uuid.uuid4().hex
        atomic_write_text(path, json.dumps(state))
    return state["device_id"]
