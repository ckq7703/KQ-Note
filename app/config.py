import json
import os

from app.store import atomic_write_text, get_data_dir

DEFAULTS = {
    "hotkey": "<ctrl>+<alt>+<space>",
    "hotkey_pin": "<ctrl>+<space>",
    "widget_geometry": "380x520+60+60",
    "always_on_top": True,
    "sync_server_url": "https://note.smartpro.com.vn",
}


def get_config_path():
    return os.path.join(get_data_dir(), "config.json")


def load_config():
    path = get_config_path()
    if not os.path.exists(path):
        return dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data or {})
    return merged


def save_config(cfg):
    atomic_write_text(get_config_path(), json.dumps(cfg, ensure_ascii=False, indent=2))
