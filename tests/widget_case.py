"""Runs the real NotesWidget under a virtual display, with the Windows-only helpers stubbed
and the sync engine's network switched off. Remote changes are injected the way the engine
would: through repo.apply_page, then the same notes_changed event the widget receives."""

import queue
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.store_case import StoreCase

ACCT = "me@example.com"


def _install_winfx_stub():
    mod = types.ModuleType("app.winfx")
    noop = lambda *a, **k: None  # noqa: E731
    for name in ("enable_dpi_awareness", "register_appbar", "unregister_appbar", "round_window"):
        setattr(mod, name, noop)
    mod.acquire_single_instance_lock = lambda: True
    mod.get_work_area = lambda *a, **k: (0, 0, 1920, 1080)
    mod.set_appbar_edge_pos = lambda *a, **k: (0, 0, 0, 0)
    mod.get_virtual_screen_rect = lambda: (0, 0, 1920, 1080)
    sys.modules["app.winfx"] = mod
    import app
    app.winfx = mod


def _install_cursor_shim():
    """Windows-only Tk cursor names (size_ns, size_nw_se, ...) are rejected by X11: map them to
    ones that exist, for tests only."""
    import tkinter as tk
    if getattr(tk, "_kq_cursor_shim", False):
        return

    def fix(options):
        c = options.get("cursor") if isinstance(options, dict) else None
        if isinstance(c, str) and c.startswith("size_"):
            options = dict(options)
            options["cursor"] = "fleur"
        return options

    orig_init, orig_configure = tk.Widget.__init__, tk.Misc._configure

    def init(self, master, widgetName, cnf={}, kw={}, extra=()):
        orig_init(self, master, widgetName, fix(cnf), fix(kw), extra)

    def configure(self, cmd, cnf, kw):
        return orig_configure(self, cmd, fix(cnf), fix(kw))

    tk.Widget.__init__, tk.Misc._configure, tk._kq_cursor_shim = init, configure, True


def remote_note(note_id="n1", rev=1, content="alpha", deleted=False, purged=False, position="V"):
    return {"id": note_id, "rev": rev, "content": content, "deleted": deleted, "purged": purged,
            "position": position, "deleted_at": "2026-01-03T00:00:00Z" if deleted else None,
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z", "title": "", "seq": rev}


class WidgetCase(StoreCase):
    """Set `logged_in = True` (and seed notes) before calling open_widget()."""

    logged_in = False

    def setUp(self):
        super().setUp()
        try:
            import PIL  # noqa: F401
            import keyring  # noqa: F401
            import tkinter as tk
            self.root = tk.Tk()
        except ImportError as e:
            self.skipTest(f"widget tests need Pillow and keyring: {e}")
        except Exception as e:  # noqa: BLE001  (TclError: no display)
            self.skipTest(f"no display available: {e}")
        self.root.withdraw()
        _install_winfx_stub()
        _install_cursor_shim()
        self.widget = None
        self._patches = []

    def tearDown(self):
        for p in self._patches:
            p.stop()
        if self.widget is not None:
            try:
                self.widget.destroy()
            except Exception:  # noqa: BLE001
                pass
        if hasattr(self, "root"):
            self.root.destroy()
        super().tearDown()

    def _patch(self, *args, **kwargs):
        p = mock.patch(*args, **kwargs)
        m = p.start()
        self._patches.append(p)
        return m

    def open_widget(self):
        from app.sync import auth_store
        state = {"logged_in": self.logged_in}
        self.auth_state = state
        self._patch.__self__  # noqa: B018
        for name, value in {
            "is_logged_in": lambda: state["logged_in"],
            "get_account_email": lambda: ACCT if state["logged_in"] else None,
            "get_access_token": lambda: "t" if state["logged_in"] else None,
            "clear": lambda: state.update(logged_in=False),
        }.items():
            self._patch.__call__  # noqa: B018
            p = mock.patch.object(auth_store, name, side_effect=value)
            p.start()
            self._patches.append(p)
        self.sync_async = self._patch("app.sync.engine.SyncEngine.sync_async")
        self.messagebox = self._patch("app.notes_widget.messagebox")
        from app import notes_widget
        self.widget = notes_widget.NotesWidget(self.root)
        self.widget.update()
        return self.widget

    # -- helpers
    def editor_text(self):
        return self.widget.text.get("1.0", "end-1c")

    def type_text(self, text):
        self.widget.text.insert("end", text)

    def remote(self, *changes):
        """Apply server notes the way the engine does, then deliver the resulting event to the widget."""
        from app.sync import repo
        events = repo.apply_page(ACCT, list(changes))
        engine = self.widget.sync_engine
        engine._flush_events(events)
        try:
            kind, payload = engine.events.get_nowait()
        except queue.Empty:
            return None
        assert kind == "notes_changed"
        self.widget._on_notes_changed(payload)
        return payload

    def account_notes(self):
        return self.sql("SELECT * FROM notes WHERE account_id = ?", (ACCT,))
