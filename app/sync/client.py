import requests

from app.version import __version__

from . import auth_store


class SyncError(Exception):
    """Generic sync failure with a human-readable message."""


class AuthRequiredError(SyncError):
    """Access and refresh tokens are both invalid/expired; user must log in again."""


class OfflineError(SyncError):
    """Network unreachable. Expected in local-first operation; caller should stay quiet."""


class RevConflict(SyncError):
    """409: the server's copy moved on. `note` is the server's current version of it."""

    def __init__(self, note):
        super().__init__("Note changed on the server")
        self.note = note


class CursorExpired(SyncError):
    """410: the change-feed cursor is no longer valid; start again from 0."""


class UpdateRequired(SyncError):
    """426: the server no longer accepts this version of the app."""


class RateLimited(SyncError):
    """429: too many requests. Not a failure of any one note: the whole cycle should back off."""


class NoteNotFound(SyncError):
    """404: the server has no such note."""


def _error_message(resp):
    try:
        data = resp.json()
        return str(data.get("detail", resp.text))
    except ValueError:
        return resp.text


class SyncClient:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers["X-Client-Version"] = __version__
        self.session.headers["User-Agent"] = f"KQNote/{__version__}"

    def _request(self, method, path, retry_auth=True, **kwargs):
        token = auth_store.get_access_token()
        headers = kwargs.pop("headers", {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self.session.request(
                method, f"{self.base_url}{path}", headers=headers, timeout=10, **kwargs
            )
        except requests.exceptions.RequestException as e:
            raise OfflineError(str(e)) from e

        if resp.status_code == 401 and retry_auth:
            if self._refresh_access_token():
                return self._request(method, path, retry_auth=False, **kwargs)
            raise AuthRequiredError("Phiên đăng nhập đã hết hạn, vui lòng đăng nhập lại")
        if resp.status_code == 426:
            raise UpdateRequired("Cần cập nhật KQ Note lên bản mới để tiếp tục đồng bộ")
        if resp.status_code == 429:
            raise RateLimited("Đang gửi quá nhiều yêu cầu tới máy chủ, sẽ thử lại sau")
        return resp

    def _refresh_access_token(self):
        refresh_token = auth_store.get_refresh_token()
        if not refresh_token:
            return False
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/refresh", json={"refresh_token": refresh_token}, timeout=10
            )
        except requests.exceptions.RequestException:
            return False
        if resp.status_code != 200:
            return False
        auth_store.set_access_token(resp.json()["access_token"])
        return True

    def register(self, email, password):
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/register",
                json={"email": email, "password": password},
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            raise OfflineError(str(e)) from e
        if resp.status_code >= 400:
            raise SyncError(_error_message(resp))
        data = resp.json()
        auth_store.set_tokens(data["access_token"], data["refresh_token"])
        auth_store.set_account_email(email)

    def login(self, email, password):
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/login",
                json={"email": email, "password": password},
                timeout=10,
            )
        except requests.exceptions.RequestException as e:
            raise OfflineError(str(e)) from e
        if resp.status_code >= 400:
            raise SyncError(_error_message(resp))
        data = resp.json()
        auth_store.set_tokens(data["access_token"], data["refresh_token"])
        auth_store.set_account_email(email)

    def login_with_google(self, auth):
        # `auth` is {"code", "code_verifier", "redirect_uri"} from the loopback
        # flow. The backend does the code->token exchange with Google (it holds
        # the OAuth client secret) so no secret ever ships in the client.
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/google",
                json=auth,
                timeout=15,
            )
        except requests.exceptions.RequestException as e:
            raise OfflineError(str(e)) from e
        if resp.status_code >= 400:
            raise SyncError(_error_message(resp))
        data = resp.json()
        auth_store.set_tokens(data["access_token"], data["refresh_token"])

    def fetch_account(self):
        resp = self._request("GET", "/auth/me")
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.json()  # {"email": ..., "avatar_url": ...}

    def get_legacy_note(self):
        """The pre-multi-note single cloud blob (read only: nothing writes to it any more)."""
        resp = self._request("GET", "/notes/me")
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.json()

    # ---- multi-note (v2) ----
    def _note_response(self, resp):
        if resp.status_code == 409:
            raise RevConflict(resp.json()["detail"]["note"])
        if resp.status_code == 404:
            raise NoteNotFound(_error_message(resp))
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.json()

    def changes(self, cursor, limit=200):
        resp = self._request("GET", "/v2/notes/changes", params={"cursor": cursor, "limit": limit})
        if resp.status_code == 410:
            raise CursorExpired("cursor_expired")
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.json()  # {"changes": [...], "cursor": int, "has_more": bool}

    def put_note(self, note_id, content, base_rev, device_id, mutation_id, position, restore=False):
        return self._note_response(self._request(
            "PUT", f"/v2/notes/{note_id}",
            json={"content": content, "base_rev": base_rev, "device_id": device_id,
                  "mutation_id": mutation_id, "position": position, "restore": restore}))

    def trash_note(self, note_id, base_rev, device_id, mutation_id):
        return self._note_response(self._request(
            "POST", f"/v2/notes/{note_id}/trash",
            json={"base_rev": base_rev, "device_id": device_id, "mutation_id": mutation_id}))

    def restore_note(self, note_id, base_rev, device_id, mutation_id):
        return self._note_response(self._request(
            "POST", f"/v2/notes/{note_id}/restore",
            json={"base_rev": base_rev, "device_id": device_id, "mutation_id": mutation_id}))

    def purge_note(self, note_id, base_rev, device_id, mutation_id):
        return self._note_response(self._request(
            "POST", f"/v2/notes/{note_id}/purge",
            json={"base_rev": base_rev, "device_id": device_id, "mutation_id": mutation_id}))

    def move_note(self, note_id, position):
        return self._note_response(self._request("PATCH", f"/v2/notes/{note_id}", json={"position": position}))

    def fetch_image_manifest(self):
        resp = self._request("GET", "/images/manifest")
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return set(resp.json()["ids"])

    def upload_image(self, file_id, image_bytes):
        resp = self._request(
            "PUT", f"/images/{file_id}", data=image_bytes,
            headers={"Content-Type": "application/octet-stream"},
        )
        if resp.status_code != 204:
            raise SyncError(_error_message(resp))

    def download_image(self, file_id):
        resp = self._request("GET", f"/images/{file_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.content
