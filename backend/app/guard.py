"""Request guard: rate limiting, the client-version gate and the legacy-API sunset.

Runs before routing, so it is cheap and never touches the database. State is in memory,
which is right for the single-process deployment (a restart just resets the counters).
"""

import hashlib
import threading
import time
from collections import deque

from fastapi import Request
from fastapi.responses import JSONResponse

from .config import settings

LEGACY_GONE = "This version of KQ Note is no longer supported. Please update the app."


class RateLimiter:
    """Sliding window: at most `limit` hits per key in the last `window` seconds."""

    def __init__(self, window=60.0):
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()
        self._next_sweep = 0.0

    def hit(self, key, limit, now=None):
        """Record a request. Returns (allowed, seconds_until_a_slot_frees)."""
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and hits[0] <= now - self.window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(hits[0] + self.window - now) + 1)
            hits.append(now)
            if now >= self._next_sweep:
                self._sweep(now)
            return True, 0

    def _sweep(self, now):
        self._next_sweep = now + self.window
        for key in [k for k, q in self._hits.items() if not q or q[-1] <= now - self.window]:
            del self._hits[key]

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    n = settings.trusted_proxy_count
    if n > 0:
        forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
        if len(forwarded) >= n:
            return forwarded[-n]  # counted from the right: only what our own proxies appended is trusted
    return peer


def _version(text):
    try:
        parts = [int(p) for p in text.strip().lstrip("vV").split(".")[:4]]
    except ValueError:
        return None
    return tuple(parts + [0] * (4 - len(parts)))


def is_older(version, minimum):
    a, b = _version(version), _version(minimum)
    return a is not None and b is not None and a < b


async def guard(request: Request, call_next):
    path = request.url.path
    if path == "/health":
        return await call_next(request)

    if path.startswith("/notes/") and not settings.legacy_notes_enabled:
        return JSONResponse(status_code=410, content={"detail": LEGACY_GONE})

    if settings.min_client_version and path.startswith("/v2/"):
        version = request.headers.get("x-client-version")
        if version and is_older(version, settings.min_client_version):
            return JSONResponse(status_code=426, content={"detail": "update_required",
                                                          "min_version": settings.min_client_version})

    if settings.rate_limit_enabled:
        if path.startswith("/auth/"):
            key, limit = f"ip:{client_ip(request)}", settings.rate_limit_auth_per_minute
        else:
            token = request.headers.get("authorization")
            key = "tok:" + hashlib.sha1(token.encode("utf-8")).hexdigest() if token else f"ip:{client_ip(request)}"
            limit = settings.rate_limit_api_per_minute
        allowed, retry_after = limiter.hit(key, limit)
        if not allowed:
            return JSONResponse(status_code=429, content={"detail": "rate_limited"},
                                headers={"Retry-After": str(retry_after)})

    response = await call_next(request)
    if path.startswith("/notes/"):
        response.headers["Deprecation"] = "true"
        if settings.legacy_sunset:
            response.headers["Sunset"] = settings.legacy_sunset
    return response
