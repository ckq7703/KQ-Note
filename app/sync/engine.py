"""Background sync orchestration.

Network calls run on daemon threads; results land on a thread-safe Queue that
the Tk main loop drains via `after()` polling (Tkinter widgets must only be
touched from the main thread).

Conflict policy (v1, intentionally simple): local edits are never silently
discarded. If the server has a newer version when we push, we stash the
server's competing content to a `notes.conflict-<timestamp>.txt` backup file
and then push local content as the new authoritative version. A real 3-way
auto-merge is a reasonable v2 improvement but was left out to avoid shipping
a hand-rolled merge algorithm that could quietly corrupt notes.

The local-only note and a logged-in account's note are treated as two
separate documents (separate files on disk): logging in always shows the
account's cloud content, logging out always shows the local-only note again,
and neither is auto-pushed into the other. `pull_async(initial=True)` exists
so login can unconditionally fetch-and-display the account's content instead
of going through the normal pull's "only apply if local is unchanged" guard,
which doesn't apply here since there's no shared "last synced" state yet.
"""

import hashlib
import queue
import threading

import requests

from app import store

from . import auth_store, state as sync_state
from .client import AuthRequiredError, OfflineError, SyncClient, SyncConflict, SyncError


def content_hash(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


class SyncEngine:
    def __init__(self, server_url):
        self.client = SyncClient(server_url)
        self.events = queue.Queue()

    # ---- account ----
    def is_logged_in(self):
        return auth_store.is_logged_in()

    def account_email(self):
        return auth_store.get_account_email()

    def register(self, email, password):
        self.client.register(email, password)
        sync_state.clear()

    def login(self, email, password):
        self.client.login(email, password)
        sync_state.clear()

    def login_with_google_async(self):
        threading.Thread(target=self._login_with_google, daemon=True).start()

    def _login_with_google(self):
        from . import google_oauth  # imported lazily: pulls in webbrowser/http.server

        try:
            google_token = google_oauth.run_oauth_flow()
            self.client.login_with_google(google_token)
            account = self.client.fetch_account()
            auth_store.set_account_email(account["email"])
            if account.get("avatar_url"):
                self._download_avatar(account["avatar_url"])
            else:
                store.clear_avatar()
            sync_state.clear()
            self.events.put(("google_login_success", None))
        except (google_oauth.GoogleLoginError, SyncError) as e:
            self.events.put(("google_login_error", str(e)))

    def _download_avatar(self, url):
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                store.save_avatar(resp.content)
        except requests.exceptions.RequestException:
            pass  # avatar is cosmetic — a failed fetch shouldn't fail the login

    def logout(self):
        auth_store.clear()
        sync_state.clear()
        store.clear_avatar()

    # ---- background operations (fire-and-forget; results via self.events) ----
    def push_async(self, content):
        threading.Thread(target=self._push, args=(content,), daemon=True).start()

    def pull_async(self, initial=False):
        threading.Thread(target=self._pull, args=(initial,), daemon=True).start()

    def _push(self, content):
        if not self.is_logged_in():
            return
        st = sync_state.load_state()
        new_hash = content_hash(content)
        if new_hash == st.get("last_synced_hash"):
            return  # nothing changed locally since the last sync

        base_version = st.get("last_synced_version") or 0
        try:
            result = self.client.put_note(content, base_version, st["device_id"])
            sync_state.update_after_sync(result["version"], content_hash(result["content"]))
            self.events.put(("synced", None))
        except SyncConflict as conflict:
            backup_path = sync_state.write_conflict_backup(conflict.content)
            try:
                result = self.client.put_note(content, conflict.version, st["device_id"])
                sync_state.update_after_sync(result["version"], content_hash(result["content"]))
                self.events.put(("conflict_resolved", backup_path))
            except SyncError as e:
                self.events.put(("error", str(e)))
        except AuthRequiredError:
            self.events.put(("auth_required", None))
        except OfflineError:
            pass  # local-first: silently retry on the next cycle
        except SyncError as e:
            self.events.put(("error", str(e)))

    def _pull(self, initial=False):
        if not self.is_logged_in():
            return
        try:
            result = self.client.get_note()
        except AuthRequiredError:
            self.events.put(("auth_required", None))
            return
        except OfflineError:
            return
        except SyncError as e:
            self.events.put(("error", str(e)))
            return

        if initial:
            # First contact after a fresh login: never race a push against this,
            # the caller decides whether to adopt server content or push local up.
            self.events.put(("initial_pull", result))
            return

        st = sync_state.load_state()
        if result["version"] == st.get("last_synced_version"):
            return  # already up to date
        self.events.put(("remote_update", result))
