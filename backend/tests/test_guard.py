from app import guard as guard_module
from app.config import settings

from conftest import new_id


def test_the_api_limit_is_per_token_and_reports_when_to_retry(client, alice, bob, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_api_per_minute", 5)
    guard_module.limiter.reset()

    codes = [client.get("/v2/notes/changes", headers=alice).status_code for _ in range(7)]

    assert codes[:5] == [200] * 5
    assert codes[5:] == [429, 429]
    limited = client.get("/v2/notes/changes", headers=alice)
    assert limited.json() == {"detail": "rate_limited"}
    assert 1 <= int(limited.headers["Retry-After"]) <= 61
    assert client.get("/v2/notes/changes", headers=bob).status_code == 200  # another token has its own budget


def test_the_limit_can_be_switched_off(client, alice, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_api_per_minute", 2)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    guard_module.limiter.reset()
    assert all(client.get("/v2/notes/changes", headers=alice).status_code == 200 for _ in range(6))


def test_health_is_never_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_api_per_minute", 1)
    guard_module.limiter.reset()
    assert all(client.get("/health").status_code == 200 for _ in range(5))


def test_login_attempts_are_limited_per_address(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 3)
    guard_module.limiter.reset()
    body = {"email": "nobody@example.com", "password": "wrong-password"}
    codes = [client.post("/auth/login", json=body).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]


def test_forwarded_addresses_count_only_from_the_trusted_proxy(client, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_auth_per_minute", 1)
    monkeypatch.setattr(settings, "trusted_proxy_count", 1)
    guard_module.limiter.reset()
    body = {"email": "nobody@example.com", "password": "wrong-password"}

    def login(xff):
        return client.post("/auth/login", json=body, headers={"X-Forwarded-For": xff}).status_code

    assert login("1.1.1.1") == 401
    assert login("1.1.1.1") == 429
    assert login("2.2.2.2") == 401  # a different real client
    # a spoofed left-hand entry does not buy a fresh budget: only the proxy's own entry is trusted
    assert login("9.9.9.9, 1.1.1.1") == 429


def test_old_clients_are_told_to_update_only_when_a_minimum_is_set(client, alice, monkeypatch):
    def changes(version=None):
        headers = dict(alice, **({"X-Client-Version": version} if version else {}))
        return client.get("/v2/notes/changes", headers=headers)

    assert changes("1.4.4").status_code == 200  # no minimum configured
    monkeypatch.setattr(settings, "min_client_version", "1.5.0")
    old = changes("1.4.4")
    assert old.status_code == 426
    assert old.json() == {"detail": "update_required", "min_version": "1.5.0"}
    assert changes("1.5.0").status_code == 200
    assert changes("1.10.2").status_code == 200  # numeric, not string, comparison
    assert changes("v2").status_code == 200
    assert changes().status_code == 200  # a client that sends no version is not blocked
    assert changes("garbage").status_code == 200


def test_the_legacy_api_is_marked_deprecated_and_can_be_retired(client, alice, monkeypatch):
    r = client.get("/notes/me", headers=alice)
    assert r.status_code == 200 and r.headers["Deprecation"] == "true" and "Sunset" not in r.headers

    monkeypatch.setattr(settings, "legacy_sunset", "Wed, 31 Dec 2026 23:59:59 GMT")
    assert client.get("/notes/me", headers=alice).headers["Sunset"] == "Wed, 31 Dec 2026 23:59:59 GMT"

    monkeypatch.setattr(settings, "legacy_notes_enabled", False)
    gone = client.get("/notes/me", headers=alice)
    assert gone.status_code == 410 and "update" in gone.json()["detail"]
    assert client.put(f"/v2/notes/{new_id()}", headers=alice,
                      json={"content": "still fine", "base_rev": 0}).status_code == 200  # v2 unaffected


def test_the_limiter_forgets_idle_keys():
    limiter = guard_module.RateLimiter(window=10)
    for i in range(50):
        assert limiter.hit(f"k{i}", 5, now=100.0)[0]
    limiter.hit("later", 5, now=1000.0)  # far past the window: the sweep runs
    assert set(limiter._hits) == {"later"}
