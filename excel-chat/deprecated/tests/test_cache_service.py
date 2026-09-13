"""
Tests for the Redis LLM cache layer (cache_service.py).

Coverage:
  * cache_get / cache_set against a mocked Redis client
  * SQLite fallback when REDIS_URL is not set
  * SQLite fallback when Redis is configured but unreachable
  * 7-day (604800s) TTL is applied to setex and expire calls
  * cache_invalidate_user stub returns 0
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add backend/src to sys.path so we can import the module under test without
# requiring it to be installed. Matches the pattern in test_multi_sheet.py.
BACKEND_SRC = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))


@pytest.fixture(autouse=True)
def _isolate_redis_state(monkeypatch):
    """Reset the module-level Redis singleton before every test.

    Each test sets its own REDIS_URL value (or omits it), so the singleton
    must be cleared between tests to avoid leaking state.
    """
    import cache_service

    monkeypatch.delenv("REDIS_URL", raising=False)
    cache_service.reset_redis_client()
    yield
    cache_service.reset_redis_client()


def _make_redis_mock():
    """Build a MagicMock that quacks like redis.Redis for our usage."""
    r = MagicMock()
    r.get.return_value = None
    r.setex.return_value = True
    r.expire.return_value = True
    r.incr.return_value = 1
    r.delete.return_value = 0
    r.scan_iter.return_value = iter([])
    return r


# ---------------------------------------------------------------------------
# cache_get / cache_set with mock Redis
# ---------------------------------------------------------------------------

def test_cache_set_writes_to_redis_with_ttl(monkeypatch):
    """cache_set must call setex with the 7-day default TTL (604800s)."""
    import cache_service

    r = _make_redis_mock()
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    with patch("cache_service.redis.from_url", return_value=r) as from_url:
        cache_service.cache_set("gpt-4", "hello", "world")

    from_url.assert_called_once_with("redis://localhost:6379/0", decode_responses=True)
    r.setex.assert_called_once()
    args, _ = r.setex.call_args
    # setex(key, ttl, value) — positional
    assert args[1] == cache_service.DEFAULT_TTL_SECONDS == 604800
    assert args[2] == "world"
    assert args[0].startswith("llm:")


def test_cache_set_uses_custom_ttl(monkeypatch):
    """A custom ttl= overrides the 7-day default."""
    import cache_service

    r = _make_redis_mock()
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    with patch("cache_service.redis.from_url", return_value=r):
        cache_service.cache_set("gpt-4", "hello", "world", ttl=60)

    args, _ = r.setex.call_args
    assert args[1] == 60


def test_cache_get_returns_redis_value_and_refreshes_ttl(monkeypatch):
    """On Redis hit, return the value, refresh TTL, increment hit counter."""
    import cache_service

    r = _make_redis_mock()
    r.get.return_value = "cached-answer"
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    with patch("cache_service.redis.from_url", return_value=r):
        result = cache_service.cache_get("gpt-4", "hello")

    assert result == "cached-answer"
    r.get.assert_called_once()
    key_used = r.get.call_args[0][0]
    assert key_used.startswith("llm:")

    # Sliding expiration: TTL must be reset on every hit.
    r.expire.assert_called_once_with(key_used, cache_service.DEFAULT_TTL_SECONDS)
    # Hit counter incremented under `{key}:hits`.
    r.incr.assert_called_once_with(f"{key_used}:hits")


def test_cache_get_returns_none_on_redis_miss(monkeypatch):
    """When Redis returns None, cache_get must return None (no SQLite call)."""
    import cache_service

    r = _make_redis_mock()
    r.get.return_value = None
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    with patch("cache_service.redis.from_url", return_value=r):
        with patch("sheet_metadata.get_cached_response") as sqlite_get:
            result = cache_service.cache_get("gpt-4", "hello")

    assert result is None
    sqlite_get.assert_not_called()
    # No expire/incr on a miss.
    r.expire.assert_not_called()
    r.incr.assert_not_called()


# ---------------------------------------------------------------------------
# SQLite fallback paths
# ---------------------------------------------------------------------------

def test_cache_get_falls_back_to_sqlite_when_redis_url_unset(monkeypatch):
    """No REDIS_URL → cache_get reads from sheet_metadata.get_cached_response."""
    import cache_service

    # REDIS_URL explicitly absent.
    monkeypatch.delenv("REDIS_URL", raising=False)

    with patch("sheet_metadata.get_cached_response", return_value="sqlite-hit") as sq_get:
        result = cache_service.cache_get("gpt-4", "hello")

    assert result == "sqlite-hit"
    sq_get.assert_called_once_with("gpt-4", "hello")


def test_cache_set_falls_back_to_sqlite_when_redis_url_unset(monkeypatch):
    """No REDIS_URL → cache_set writes to sheet_metadata.set_cached_response."""
    import cache_service

    monkeypatch.delenv("REDIS_URL", raising=False)

    with patch("sheet_metadata.set_cached_response") as sq_set:
        cache_service.cache_set("gpt-4", "hello", "world")

    sq_set.assert_called_once_with("gpt-4", "hello", "world")


def test_cache_get_falls_back_to_sqlite_when_redis_raises(monkeypatch):
    """Connection errors must degrade gracefully to SQLite."""
    import cache_service

    monkeypatch.setenv("REDIS_URL", "redis://unreachable:6379/0")

    # redis.from_url succeeds, but every subsequent call blows up.
    broken = MagicMock()
    broken.get.side_effect = ConnectionError("redis is down")
    with patch("cache_service.redis.from_url", return_value=broken):
        with patch("sheet_metadata.get_cached_response", return_value="sqlite-fallback") as sq_get:
            result = cache_service.cache_get("gpt-4", "hello")

    assert result == "sqlite-fallback"
    sq_get.assert_called_once()


def test_cache_set_falls_back_to_sqlite_when_redis_raises(monkeypatch):
    """Set-side connection errors must degrade gracefully too."""
    import cache_service

    monkeypatch.setenv("REDIS_URL", "redis://unreachable:6379/0")

    broken = MagicMock()
    broken.setex.side_effect = ConnectionError("redis is down")
    with patch("cache_service.redis.from_url", return_value=broken):
        with patch("sheet_metadata.set_cached_response") as sq_set:
            cache_service.cache_set("gpt-4", "hello", "world")

    sq_set.assert_called_once_with("gpt-4", "hello", "world")


# ---------------------------------------------------------------------------
# cache_invalidate_user stub
# ---------------------------------------------------------------------------

def test_cache_invalidate_user_stub_returns_zero():
    """Stub for the future per-user semantic cache. Always returns 0 today."""
    import cache_service

    assert cache_service.cache_invalidate_user("user-123") == 0
    # Empty user_id is a no-op too.
    assert cache_service.cache_invalidate_user("") == 0


# ---------------------------------------------------------------------------
# Key derivation — guard against silent prompt-key divergence
# ---------------------------------------------------------------------------

def test_cache_key_matches_sheet_metadata_scheme():
    """Redis key SHA-256 must match the SQLite scheme exactly.

    If these ever diverge, a write to Redis won't be readable from SQLite (or
    vice versa) and we'll silently duplicate entries.
    """
    import cache_service

    # Importing sheet_metadata triggers load_dotenv() which is fine for tests.
    from sheet_metadata import _cache_key as sqlite_cache_key

    model, prompt = "gpt-4", "describe sheet X"
    sqlite_key = sqlite_cache_key(model, prompt)
    redis_key = cache_service._redis_key(model, prompt)

    # The Redis key is `llm:` + the SQLite key.
    assert redis_key == f"llm:{sqlite_key}"


# ---------------------------------------------------------------------------
# Singleton behavior — _get_redis caches its client
# ---------------------------------------------------------------------------

def test_get_redis_is_singleton(monkeypatch):
    """_get_redis must call redis.from_url exactly once per process."""
    import cache_service

    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    r = _make_redis_mock()
    with patch("cache_service.redis.from_url", return_value=r) as from_url:
        client1 = cache_service._get_redis()
        client2 = cache_service._get_redis()
        client3 = cache_service._get_redis()

    assert client1 is client2 is client3 is r
    assert from_url.call_count == 1


def test_get_redis_returns_none_when_url_unset(monkeypatch):
    """No REDIS_URL → _get_redis returns None (caller decides fallback)."""
    import cache_service

    monkeypatch.delenv("REDIS_URL", raising=False)
    assert cache_service._get_redis() is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
