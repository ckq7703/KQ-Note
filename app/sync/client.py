import requests

from . import auth_store


class SyncError(Exception):
    """Generic sync failure with a human-readable message."""


class AuthRequiredError(SyncError):
    """Access and refresh tokens are both invalid/expired; user must log in again."""


class OfflineError(SyncError):
    """Network unreachable. Expected in local-first operation; caller should stay quiet."""


class SyncConflict(SyncError):
    def __init__(self, content, version, updated_at):
        super().__init__("Version conflict")
        self.content = content
        self.version = version
        self.updated_at = updated_at


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

    def login_with_google(self, google_id_token):
        try:
            resp = self.session.post(
                f"{self.base_url}/auth/google",
                json={"id_token": google_id_token},
                timeout=10,
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

    def get_note(self):
        resp = self._request("GET", "/notes/me")
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.json()

    def put_note(self, content, base_version, device_id):
        resp = self._request(
            "PUT",
            "/notes/me",
            json={"content": content, "base_version": base_version, "device_id": device_id},
        )
        if resp.status_code == 409:
            data = resp.json()["detail"]
            raise SyncConflict(data["content"], data["version"], data["updated_at"])
        if resp.status_code != 200:
            raise SyncError(_error_message(resp))
        return resp.json()
