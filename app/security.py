"""Lightweight request-level protections for POST /query: an optional API key, and a per-client
rate limit. Neither is part of the SQL-safety defense (that's the validator + read-only role); these
exist so a single deployment cannot be trivially hammered or used anonymously if the operator wants
to lock it down.

Honest limitation: the rate limiter is a plain in-memory counter, scoped to one process. It is
correct for a single-instance demo deployment (this project's target) and would need a shared store
(e.g. Redis) behind more than one worker process or replica -- see docs/NL_SQL_SECURITY.md.
"""
from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque


class RateLimitExceeded(Exception):
    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds


class RateLimiter:
    """Sliding-window limiter: at most `limit` calls per `window_seconds` per key."""

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = max(1, int(self.window_seconds - (now - hits[0])))
                raise RateLimitExceeded(retry_after)
            hits.append(now)


def check_api_key(configured_key: str | None, provided_key: str | None) -> bool:
    """Return True if the request is authorized. If no key is configured, the endpoint is open
    (documented in the README as the default for local/demo use) -- operators opt into locking it
    down by setting API_KEY."""
    if configured_key is None:
        return True
    return provided_key is not None and hmac.compare_digest(provided_key, configured_key)
