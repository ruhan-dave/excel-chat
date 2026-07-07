"""
Redis LLM cache with SQLite fallback.

Uses REDIS_URL env var to connect to Redis (Upstash, ElastiCache, local Docker).
If REDIS_URL is unset or unreachable, all operations transparently fall back
to the SQLite `llm_cache` table managed by sheet_metadata.
"""

from __future__ import annotations

import hashlib
import os

# `redis` is an optional dependency at runtime: if REDIS_URL is unset we never
# touch it, so import errors here should not break the app.
try:
    import redis  # type: ignore
except ImportError:  # pragma: no cover
    redis = None  # type: ignore

# 7-day TTL — used for every cache write. Rationale: covers a full work-week of
# interactive sessions without unbounded growth; sliding expiration on read
# keeps hot keys alive longer.
DEFAULT_TTL_SECONDS = 604800

# Module-level singleton. Lazy-initialized so importing this module has zero
# side effects.
_redis_client = None


def _get_redis():
    """Return a process-wide Redis client, or None if REDIS_URL is unset.

    Connection failures bubble up to the caller; cache_get/cache_set catch them
    and fall back to SQLite. We deliberately do NOT silently swallow errors
    inside _get_redis — only callers that want fallback behavior should wrap
    the call in try/except.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    url = os.environ.get("REDIS_URL")
    if not url:
        return None
    if redis is None:
        # redis package not installed but REDIS_URL is set — refuse to silently
        # use SQLite, since that would hide a misconfigured deployment.
        raise RuntimeError(
            "REDIS_URL is set but the 'redis' package is not installed. "
            "Run `pip install 'redis>=5.0.0'`."
        )
    _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client


def _cache_key(model: str, prompt: str) -> str:
    """SHA-256 of `model:prompt` — same scheme used by sheet_metadata."""
    raw = f"{model}:{prompt}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _redis_key(model: str, prompt: str) -> str:
    """Redis key for a cached LLM response."""
    return f"llm:{_cache_key(model, prompt)}"


def reset_redis_client() -> None:
    """Clear the cached client. Test helper — not used in production."""
    global _redis_client
    _redis_client = None


def cache_get(model: str, prompt: str) -> str | None:
    """Return a cached LLM response for (model, prompt), or None on miss.

    Lookup order:
      1. Redis (if REDIS_URL is set and reachable)
      2. SQLite `llm_cache` table

    On a Redis hit the TTL is refreshed (sliding expiration) and a hit counter
    is incremented, mirroring the SQLite behavior so observability tools that
    read either store see the same metrics.
    """
    key = _redis_key(model, prompt)
    try:
        r = _get_redis()
        if r is not None:
            val = r.get(key)
            if val is not None:
                try:
                    r.incr(f"{key}:hits")
                    r.expire(key, DEFAULT_TTL_SECONDS)
                except Exception:
                    pass
                return val
            # Redis miss — fall through to SQLite (may have data from dual-write)
    except Exception as e:
        print(f"⚠️ Redis cache_get failed, falling back to SQLite: {e}")
        reset_redis_client()

    # SQLite fallback / source of truth.
    from sheet_metadata import get_cached_response
    return get_cached_response(model, prompt)


def cache_set(model: str, prompt: str, response: str, ttl: int = DEFAULT_TTL_SECONDS) -> None:
    """Store an LLM response in the cache with the given TTL (default 7 days).

    Dual-write: writes to Redis (when available) AND SQLite (source of truth).
    Failures on either backend are logged but never raised — caching is
    best-effort and a failed write must not break the LLM call path.
    """
    key = _redis_key(model, prompt)
    redis_ok = False
    try:
        r = _get_redis()
        if r is not None:
            r.setex(key, ttl, response)
            redis_ok = True
    except Exception as e:
        print(f"⚠️ Redis cache_set failed: {e}")
        reset_redis_client()

    # SQLite write — always (source of truth, not just fallback).
    try:
        from sheet_metadata import set_cached_response
        set_cached_response(model, prompt, response)
    except Exception as e:
        if not redis_ok:
            print(f"⚠️ Both Redis and SQLite cache_set failed: {e}")
        else:
            print(f"⚠️ SQLite cache_set failed (Redis write succeeded): {e}")


def cache_invalidate_user(user_id: str) -> int:
    """Invalidate all cache entries for a user.

    Delegates to semantic_cache.invalidate_user_cache, which fans out to:
    - Redis semantic:* keys (SCAN + DELETE)
    - Redis result:* and sandbox:* keys (via result_cache.invalidate_user_results)
    - SQLite llm_cache rows for the user
    - SQLite result_cache rows for the user

    Exact-match cache keys (llm:{hash}) are intentionally NOT invalidated —
    they are keyed by (model, prompt), not user, and identical prompts should
    reuse responses across users (LLM responses don't contain user-specific
    data).

    Returns the total number of entries deleted across all stores.
    """
    if not user_id:
        return 0
    try:
        from semantic_cache import invalidate_user_cache as semantic_invalidate
        return semantic_invalidate(user_id)
    except Exception as e:
        print(f"⚠️ cache_invalidate_user failed: {e}")
        return 0
