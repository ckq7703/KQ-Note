"""Test rig for the sync engine: the REAL backend in a subprocess and several simulated
devices (each with its own data directory and login tokens) talking to it over HTTP.

Needs a Python that has the backend's requirements (fastapi, uvicorn, sqlalchemy, ...) plus
requests and Pillow; tests skip themselves when those are missing.
"""

import importlib
import os
import queue
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, ROOT)


def backend_available():
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        import sqlalchemy  # noqa: F401
        import keyring  # noqa: F401
    except ImportError:
        return False
    return True


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """The real API on a scratch SQLite file."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "server.db")
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ, DATABASE_URL=f"sqlite:///{self.db_path}",
                        JWT_SECRET_KEY="test-secret", PYTHONDONTWRITEBYTECODE="1")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port),
             "--log-level", "warning"],
            cwd=BACKEND, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if requests.get(f"{self.url}/health", timeout=1).status_code == 200:
                    return
            except requests.exceptions.RequestException:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("test server did not start")

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.tmp.cleanup()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, params).fetchall()
            conn.commit()
            return rows
        finally:
            conn.close()

    def notes(self, email):
        return self.query(
            "SELECT n.* FROM notes n JOIN users u ON u.id = n.user_id WHERE u.email = ? ORDER BY n.seq", (email,))

    def run_backend_code(self, code):
        """Run a snippet inside the backend package against the same database."""
        subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=self.env, check=True,
                       stdout=subprocess.DEVNULL)


class FlakySession:
    """Wraps requests.Session so a test can cut the network or lose one response."""

    def __init__(self, real):
        self.real = real
        self.offline = False
        self.drop_response_when = None  # one-shot predicate (method, url) -> bool

    def request(self, method, url, **kw):
        if self.offline:
            raise requests.exceptions.ConnectionError("offline (test)")
        resp = self.real.request(method, url, **kw)
        if self.drop_response_when and self.drop_response_when(method, url):
            self.drop_response_when = None
            raise requests.exceptions.ConnectionError("response lost (test)")
        return resp

    def post(self, url, **kw):
        if self.offline:
            raise requests.exceptions.ConnectionError("offline (test)")
        return self.real.post(url, **kw)


_current = {"device": None}


def _install_fake_auth():
    """Replace the OS keyring with per-device in-memory storage."""
    from app.sync import auth_store

    def tokens():
        return _current["device"].secrets

    auth_store.set_tokens = lambda a, r: tokens().update(access=a, refresh=r)
    auth_store.set_access_token = lambda a: tokens().update(access=a)
    auth_store.get_access_token = lambda: tokens().get("access")
    auth_store.get_refresh_token = lambda: tokens().get("refresh")
    auth_store.set_account_email = lambda e: tokens().update(email=e or "")
    auth_store.get_account_email = lambda: tokens().get("email") or None
    auth_store.is_logged_in = lambda: bool(tokens().get("access"))
    auth_store.clear = lambda: tokens().clear()


class Device:
    """One installation: own data dir, own login, own engine. Only one is 'active' at a time
    (the store keeps process-wide state), so every action goes through activate()."""

    def __init__(self, name, server_url):
        self.name = name
        self.appdata = tempfile.mkdtemp(prefix=f"kq-{name}-")
        self.secrets = {}
        self.scope = None
        self.server_url = server_url
        self.engine = None
        self.activate()
        from app.sync.engine import SyncEngine
        self.engine = SyncEngine(server_url)
        self.session = FlakySession(self.engine.client.session)
        self.engine.client.session = self.session

    def activate(self):
        _current["device"] = self
        os.environ["APPDATA"] = self.appdata
        _install_fake_auth()
        from app import store
        self.store = importlib.reload(store)
        self.store.set_scope(self.scope)
        return self.store

    # ---- account
    def login(self, email, password="password-123", register=False):
        self.activate()
        (self.engine.register if register else self.engine.login)(email, password)

    def logout(self):
        self.activate()
        self.engine.logout()
        self.scope = None
        self.store.set_scope(None)

    # ---- syncing (mimics what the widget does with the engine's events)
    def sync(self):
        self.activate()
        self.engine.sync_once()
        events = []
        while True:
            try:
                events.append(self.engine.events.get_nowait())
            except queue.Empty:
                break
        for kind, payload in events:
            if kind == "account_ready":
                self.scope = payload
                self.store.set_scope(payload)
                self.store.ensure_note()
        return self.engine.status(), events

    # ---- what the user does
    def create(self, content):
        return self.activate().create_note(content)

    def edit(self, note_id, content):
        self.activate().save_note_by_id(note_id, content)

    def trash(self, note_id):
        self.activate().delete_note_by_id(note_id)

    def restore(self, note_id):
        return self.activate().restore_note(note_id)

    def reorder(self, ids):
        self.activate().reorder_notes(ids)

    # ---- what is on this device
    def rows(self, scope="mine"):
        st = self.activate()
        account = self.scope if scope == "mine" else scope
        conn = sqlite3.connect(st.get_db_path())
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute("SELECT * FROM notes WHERE account_id IS ? ORDER BY position, id", (account,)).fetchall()
        finally:
            conn.close()

    def live(self):
        return [r for r in self.rows() if r["deleted_at"] is None]

    def trashed(self):
        return [r for r in self.rows() if r["deleted_at"] is not None]

    def live_texts(self):
        return sorted(r["content"] for r in self.live())

    # A device may hold a starter note ("# Ghi chú mới") that is deliberately never uploaded;
    # cross-device comparisons look only at notes that have reached the server.
    def synced_rows(self):
        return [r for r in self.rows() if r["server_rev"] > 0]

    def synced_live(self):
        return [r for r in self.synced_rows() if r["deleted_at"] is None]

    def synced_texts(self):
        return sorted(r["content"] for r in self.synced_live())

    def unsynced_rows(self):
        return [r for r in self.rows() if r["server_rev"] == 0]

    def all_text(self):
        return "\n".join(r["content"] for r in self.rows())


class SyncCase(unittest.TestCase):
    server = None

    @classmethod
    def setUpClass(cls):
        if not backend_available():
            raise unittest.SkipTest("needs the backend's dependencies (run with the backend venv)")
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.stop()

    def setUp(self):
        self._old_appdata = os.environ.get("APPDATA")
        self._n = getattr(SyncCase, "_counter", 0) + 1
        SyncCase._counter = self._n
        self.email = f"user{self._n}-{int(time.time() * 1000) % 100000}@example.com"

    def tearDown(self):
        if self._old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._old_appdata

    def device(self, name, register=False):
        d = Device(name, self.server.url)
        d.login(self.email, register=register)
        return d

    def server_notes(self):
        return self.server.notes(self.email)
