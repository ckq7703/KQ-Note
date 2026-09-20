"""Background sync orchestration for the multi-note protocol (/v2/notes).

One cycle = pull what changed on the server, copy any local-only notes into the account,
push what this device still has to say. The decisions live in repo.py; this file is the
I/O around them: threads, requests, retries and the events the UI listens to.

Nothing is ever overwritten silently. A server that says "your copy is stale" (409) makes
repo.py keep the local text as a conflict copy; a note that changed on both sides is
resolved the same way; and a failure at any point just leaves the work to be derived
again on the next cycle.

Network calls run on daemon threads; results land on a thread-safe Queue that the Tk main
loop drains via `after()` polling (Tkinter widgets must only be touched from the main
thread). Events put on `events`:
  ("sync_status", {...})     state: off|syncing|synced|offline|error, message, last_ok, pending
  ("notes_changed", {...})   changed: [ids], conflicts: [...], restored: [ids]
  ("account_ready", id)      first successful pull of this session: safe to show the account's notes
  ("google_login_success", None) / ("google_login_error", msg) / ("auth_required", None)
"""

import hashlib
import os
import queue
import re
import threading
import time

import requests

from app import store

from . import auth_store, repo, state as sync_state
from .client import AuthRequiredError, CursorExpired, NoteNotFound, OfflineError, RevConflict, SyncClient, SyncError

_IMAGE_ID_RE = re.compile(r"!\[\]\(kqnote-image:([^)]+)\)|\[\[image:([^\]]+)\]\]")

PAGE_SIZE = 200
MAX_STEPS_PER_NOTE = 6  # server calls for one note per cycle; whatever is left waits for the next cycle


def _extract_image_ids(content):
    ids = set()
    for m in _IMAGE_ID_RE.finditer(content or ""):
        ids.add(m.group(1) or m.group(2))
    return ids


class SyncEngine:
    def __init__(self, server_url):
        self.client = SyncClient(server_url)
        self.events = queue.Queue()
        self.device_id = sync_state.get_device_id()
        self._cycle_lock = threading.Lock()
        self._flag_lock = threading.Lock()
        self._running = False
        self._rerun = False
        self._session_ready = False
        self._manifest = None
        self._status = {"state": "off", "message": "", "last_ok": None, "pending": 0}

    # ---- account ----
    def is_logged_in(self):
        return auth_store.is_logged_in()

    def account_email(self):
        return auth_store.get_account_email()

    def account_id(self):
        """The key notes are filed under on this device (the account's email, lower-cased)."""
        email = self.account_email()
        return email.strip().lower() if email else None

    def register(self, email, password):
        self.client.register(email, password)

    def login(self, email, password):
        self.client.login(email, password)

    def login_with_google_async(self):
        threading.Thread(target=self._login_with_google, daemon=True).start()

    def _login_with_google(self):
        from . import google_oauth  # imported lazily: pulls in webbrowser/http.server

        try:
            auth = google_oauth.run_oauth_flow()
            self.client.login_with_google(auth)
            account = self.client.fetch_account()
            auth_store.set_account_email(account["email"])
            if account.get("avatar_url"):
                self._download_avatar(account["avatar_url"])
            else:
                store.clear_avatar()
            self.events.put(("google_login_success", None))
        except (google_oauth.GoogleLoginError, SyncError) as e:
            self.events.put(("google_login_error", str(e)))
        except Exception as e:  # never let the daemon thread die silently
            self.events.put((
                "google_login_error",
                f"Lỗi không xác định khi đăng nhập Google: {e}",
            ))

    def _download_avatar(self, url):
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                store.save_avatar(resp.content)
        except requests.exceptions.RequestException:
            pass  # avatar is cosmetic — a failed fetch shouldn't fail the login

    def logout(self):
        auth_store.clear()
        store.clear_avatar()
        self._session_ready = False
        self._set_status("off")

    # ---- status ----
    def status(self):
        with self._flag_lock:
            return dict(self._status)

    def _set_status(self, state, message="", **extra):
        with self._flag_lock:
            self._status.update(state=state, message=message, **extra)
            snapshot = dict(self._status)
        self.events.put(("sync_status", snapshot))

    # ---- running a cycle ----
    def sync_async(self):
        """Run a cycle on a background thread; requests made meanwhile collapse into one rerun."""
        with self._flag_lock:
            if self._running:
                self._rerun = True
                return
            self._running = True
        threading.Thread(target=self._run_until_idle, daemon=True).start()

    def _run_until_idle(self):
        while True:
            try:
                self.sync_once()
            except BaseException:
                with self._flag_lock:
                    self._running = False
                raise
            # Deciding to stop and clearing the flag happen in one critical section, so a
            # request that arrives at that moment either reruns us or starts a new thread.
            with self._flag_lock:
                if not self._rerun:
                    self._running = False
                    return
                self._rerun = False

    def sync_once(self):
        """One synchronous cycle (also what tests call). Returns the resulting status."""
        with self._cycle_lock:
            self._cycle()
        return self.status()

    def _cycle(self):
        if not self.is_logged_in():
            self._set_status("off")
            return
        events = []
        account = None
        self._manifest = None
        try:
            self._set_status("syncing")
            account = self._ensure_account()
            self._pull(account, events)
            store.copy_local_notes_to_account(account, skip_duplicates=True)
            self._import_legacy_slot(account, events)
            if not self._session_ready:
                self._session_ready = True
                self._flush_events(events)  # so the UI has the notes before it switches to them
                self.events.put(("account_ready", account))
            self._flush_events(events)
            failures, last_error = self._push(account, events)
            repo.set_state(account, last_sync_at=int(time.time()))
            pending = len(repo.push_candidates(account))
            if failures:
                self._set_status("error", last_error, pending=pending)
            else:
                self._set_status("synced", "", last_ok=int(time.time()), pending=pending)
        except OfflineError:
            self._set_status("offline", "Không có kết nối tới máy chủ")
        except AuthRequiredError:
            self.events.put(("auth_required", None))
            self._set_status("off")
        except SyncError as e:
            self._set_status("error", str(e))
        except Exception as e:  # never let the daemon thread die silently
            self._set_status("error", f"Lỗi không xác định khi đồng bộ: {e}")
        finally:
            self._flush_events(events)

    def _ensure_account(self):
        account = self.account_id()
        if account:
            return account
        info = self.client.fetch_account()  # an older login that never recorded its email
        auth_store.set_account_email(info["email"])
        return self.account_id()

    def _flush_events(self, events):
        if not events:
            return
        changed, conflicts, restored = set(), [], []
        for e in events:
            changed.add(e["id"])
            if e["type"] == "conflict":
                conflicts.append(e)
                changed.add(e["copy_id"])
            elif e["type"] == "restored":
                restored.append(e["id"])
        events.clear()
        self.events.put(("notes_changed", {"changed": sorted(changed), "conflicts": conflicts, "restored": restored}))

    # ---- pull ----
    def _pull(self, account, events):
        cursor = repo.get_state(account)["cursor"]
        full = cursor == 0  # reading the feed from scratch: whatever it doesn't mention is gone
        seen = set()
        restarts = 0
        while True:
            try:
                page = self.client.changes(cursor, PAGE_SIZE)
            except CursorExpired:
                restarts += 1
                if restarts > 2:
                    raise SyncError("Máy chủ liên tục từ chối con trỏ đồng bộ")
                cursor, full, seen = 0, True, set()
                continue
            changes = page["changes"]
            seen.update(n["id"] for n in changes)
            # A from-scratch pass only saves its cursor at the very end, so a crash halfway
            # just repeats it instead of skipping the "gone" check.
            events.extend(repo.apply_page(account, changes, None if full else page["cursor"]))
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        if full:
            events.extend(repo.finish_full_pass(account, seen, cursor))
        self._download_images_for(events)

    def _import_legacy_slot(self, account, events):
        if repo.get_state(account)["legacy_imported"]:
            return
        try:
            data = self.client.get_legacy_note()
        except OfflineError:
            raise
        except SyncError:
            return  # not essential: try again next cycle
        created = repo.import_legacy_slot(account, data.get("content", ""))
        if created:
            events.append({"type": "changed", "id": created})

    # ---- push ----
    def _push(self, account, events):
        failures, last_error = 0, ""
        for note_id in repo.push_candidates(account):
            try:
                self._push_note(account, note_id, events)
            except (OfflineError, AuthRequiredError):
                raise
            except SyncError as e:  # one bad note must not block the others
                failures += 1
                last_error = str(e)
        return failures, last_error

    def _push_note(self, account, note_id, events):
        for _ in range(MAX_STEPS_PER_NOTE):
            row = repo.get_note(note_id)
            if row is None or row["account_id"] != account:
                return
            op = repo.next_op(row)
            if op is None:
                return
            try:
                self._do_op(row, op)
            except RevConflict as conflict:
                # The server's copy is newer: reconcile (possibly making a conflict copy), then re-plan.
                events.extend(repo.apply_page(account, [conflict.note], None))
            except NoteNotFound:
                repo.reset_unsynced(note_id)  # the server lost it: upload it again as new

    def _do_op(self, row, op):
        note_id, rev = row["id"], row["server_rev"]
        kind = op[0]
        if kind == "put":
            content = row["content"]
            self._upload_missing_images(content)
            digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
            # The same text on the same revision always has the same mutation id, so a retry
            # after a lost response is recognised by the server instead of applied twice.
            resp = self.client.put_note(note_id, content, rev, self.device_id, f"c{digest}.{rev}",
                                        row["position"], restore=op[1])
            repo.record_server_note(note_id, resp, pushed_content=content)
        elif kind == "trash":
            repo.record_server_note(note_id, self.client.trash_note(note_id, rev, self.device_id, f"t.{rev}"))
        elif kind == "restore":
            repo.record_server_note(note_id, self.client.restore_note(note_id, rev, self.device_id, f"r.{rev}"))
        elif kind == "move":
            repo.record_server_note(note_id, self.client.move_note(note_id, row["position"]))

    # ---- image blobs referenced by the note content (kqnote-image:<id>) ----
    def _upload_missing_images(self, content):
        ids = _extract_image_ids(content)
        if not ids:
            return
        if self._manifest is None:
            self._manifest = self.client.fetch_image_manifest()
        for file_id in ids - self._manifest:
            path = os.path.join(store.get_images_dir(), f"{file_id}.png")
            if not os.path.exists(path):
                continue  # referenced but never actually pasted/saved locally
            with open(path, "rb") as f:
                data = f.read()
            self.client.upload_image(file_id, data)
            self._manifest.add(file_id)

    def _download_images_for(self, events):
        """Best effort: the editor also fetches lazily, and a failure here must not fail the pull."""
        try:
            for note_id in {e["id"] for e in events}:
                row = repo.get_note(note_id)
                if row:
                    self._download_missing_images(row["content"])
        except SyncError:
            pass

    def _download_missing_images(self, content):
        for file_id in _extract_image_ids(content):
            path = os.path.join(store.get_images_dir(), f"{file_id}.png")
            if os.path.exists(path):
                continue
            data = self.client.download_image(file_id)
            if data is not None:
                with open(path, "wb") as f:
                    f.write(data)
