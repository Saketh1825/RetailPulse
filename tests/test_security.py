"""Unit tests for app/security.py -- no database or network needed."""
from __future__ import annotations

import time

import pytest

from app.security import RateLimiter, RateLimitExceeded, check_api_key


def test_no_key_configured_means_open():
    assert check_api_key(None, None) is True
    assert check_api_key(None, "anything") is True


def test_correct_key_is_accepted():
    assert check_api_key("secret", "secret") is True


@pytest.mark.parametrize("provided", [None, "", "wrong", "secre", "secrett"])
def test_missing_or_wrong_key_is_rejected(provided):
    assert check_api_key("secret", provided) is False


def test_rate_limiter_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        limiter.check("client-a")
    with pytest.raises(RateLimitExceeded):
        limiter.check("client-a")


def test_rate_limiter_is_per_key():
    limiter = RateLimiter(limit=1, window_seconds=60)
    limiter.check("client-a")
    limiter.check("client-b")  # different key, independent budget
    with pytest.raises(RateLimitExceeded):
        limiter.check("client-a")


def test_rate_limiter_recovers_after_the_window_passes():
    limiter = RateLimiter(limit=1, window_seconds=0.2)
    limiter.check("client-a")
    with pytest.raises(RateLimitExceeded):
        limiter.check("client-a")
    time.sleep(0.25)
    limiter.check("client-a")  # window has slid; should succeed again
