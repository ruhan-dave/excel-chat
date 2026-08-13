"""
Per-user semantic cache for LLM responses.

Caches queries by *semantic similarity* (cosine similarity of sentence
embeddings) instead of exact-match, so paraphrased queries like
``"Revenue in 2022"`` and ``"2022 revenue"`` hit the same cache entry.

User isolation
--------------
Every lookup and store is scoped by ``user_id``. User A's "revenue" query
must NEVER return User B's cached response — they have different sheets,
different data, different answers.

Storage tiers
-------------
1. **Redis Stack (RediSearch)** with ``FT.SEARCH`` KNN vector query when
   ``REDIS_URL`` is configured. The index stores embedding + response +
   user_id; similarity is computed server-side via the ``COSINE`` distance
   metric. TTL is 7 days, refreshed on every hit.
2. **SQLite fallback**: ``embedding_json`` column on ``llm_cache``. Cosine
   similarity is computed in NumPy against every cached embedding for the
   user. Works up to ~10K entries per user; switch to Redis for scale.

Lazy loading
------------
The SentenceTransformer model (``all-MiniLM-L6-v2``, ~80 MB) is loaded
on first use, NOT at import time. ``import semantic_cache`` is side-effect-free
even if the model isn't downloaded yet.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any

import numpy as np

from cache_service import DEFAULT_TTL_SECONDS, _get_redis

# Optional imports. ``sentence_transformers`` is required for embeddings but
# the app must still import this module without crashing if it's missing.
try:
    from sentence_transformers import SentenceTransformer  # type: ignore

    _SENTENCE_TRANSFORMERS_AVAILABLE = True
except ImportError:  # pragma: no cover
    SentenceTransformer = None  # type: ignore
    _SENTENCE_TRANSFORMERS_AVAILABLE = False

# Lazy imports for sheet_metadata happen inside functions to avoid circular
# import (semantic_cache -> sheet_metadata -> ... -> back).

# ---------------------------------------------------------------------------
# Embedding model singleton
# ---------------------------------------------------------------------------

_MODEL_NAME = "all-MiniLM-L6-v2"
_EMBED_DIM = 384  # MiniLM native dim is 384

_model_lock = threading.Lock()
_model: Any | None = None
_model_load_failed = False


def _get_model():
    """Return the SentenceTransformer model, loading it on first call.

    Returns None if sentence-transformers isn't installed or the download
    failed. Callers must check for None and skip semantic caching in that
    case — the exact-match cache will still work.
    """
    global _model, _model_load_failed
    if _model is not None:
        return _model
    if _model_load_failed:
        return None
    if not _SENTENCE_TRANSFORMERS_AVAILABLE:
        return None
    with _model_lock:
        if _model is not None:
            return _model
        try:
            _model = SentenceTransformer(_MODEL_NAME)
        except Exception as e:  # network error, corrupt cache, etc.
            _model_load_failed = True
            print(
                f"⚠️ Failed to load sentence-transformers model "
                f"'{_MODEL_NAME}': {e}. Semantic cache disabled."
            )
            return None
        return _model


def reset_model() -> None:
    """Test helper — drop the cached model and reset the load-failed flag."""
    global _model, _model_load_failed
    _model = None
    _model_load_failed = False


def embed_query(text: str) -> np.ndarray | None:
    """Embed a single query into a 384-dim L2-normalized NumPy vector.

    Returns None if the model isn't available. Callers should treat None as
    "semantic caching is unavailable; skip this lookup".
    """
    model = _get_model()
    if model is None:
        return None
    vec = model.encode(text, normalize_embeddings=True, convert_to_numpy=True)
    # ``convert_to_numpy`` returns float32; cast to float64 for downstream
    # JSON serialization compatibility.
    return vec.astype(np.float64)


# ---------------------------------------------------------------------------
# Redis Stack (RediSearch) vector index helpers
# ---------------------------------------------------------------------------

_REDIS_INDEX = "idx:semantic_cache"
_REDIS_KEY_PREFIX = "semantic:"


def _redis_key_for_user(user_id: str, cache_key: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{user_id}:{cache_key}"


def _redis_user_keys(user_id: str) -> str:
    """Glob pattern that matches every semantic-cache key for ``user_id``."""
    return f"{_REDIS_KEY_PREFIX}{user_id}:*"


def _ensure_redis_index(r: Any) -> bool:
    """Create the RediSearch index for vector search if it doesn't exist.

    Returns True if the index is available (existed or just created).
    Returns False if the connected Redis doesn't support RediSearch.
    """
    try:
        # FT().info() raises if the index doesn't exist on Redis Stack.
        r.ft(_REDIS_INDEX).info()
        return True
    except Exception:
        pass
    try:
        r.ft(_REDIS_INDEX).create_index(
            fields=[
                "user_id",
                "query",
                "model",
                "response",
                "embedding",
            ],
            definition={
                "type": "FLAT",
                "DIM": _EMBED_DIM,
                "DISTANCE_METRIC": "COSINE",
            },
        )
        return True
    except Exception:
        # RediSearch not available — caller falls back to SQLite.
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _extract_years(text: str) -> set[str]:
    """Extract 4-digit year-like numbers from text.

    Matches years in the range 1900-2099. Returns a set of string
    representations so that ``"2020"`` and ``2020`` compare equal.
    """
    return set(re.findall(r"\b(19\d{2}|20\d{2})\b", text))


def _extract_numbers(text: str) -> set[str]:
    """Extract all standalone numbers (integers and floats) from text.

    Returns a set of canonical string forms (e.g. ``"3.5"``, ``"42"``).
    Excludes 4-digit years (handled by ``_extract_years``).
    """
    nums = set()
    for match in re.finditer(r"(?<!\w)(\d+\.?\d*)(?!\w)", text):
        val = match.group(1)
        # Skip 4-digit years — they're handled separately
        if re.fullmatch(r"19\d{2}|20\d{2}", val):
            continue
        try:
            # Normalize: int if whole number, float otherwise
            f = float(val)
            if f == int(f) and "." not in val:
                nums.add(str(int(f)))
            else:
                nums.add(str(f))
        except ValueError:
            continue
    return nums


def _numbers_match(query_nums: set[str], cached_nums: set[str]) -> bool:
    """Deterministic exact-match check for numbers and years.

    Returns True if the sets are identical. If the *query* has no
    numbers/years (e.g. ``query_text`` was not provided), no numeric
    constraint is enforced — the semantic similarity handles it alone.
    """
    if not query_nums:
        return True
    return query_nums == cached_nums


def find_similar_cached(
    user_id: str,
    query_embedding: np.ndarray | None,
    threshold: float = 0.88,
    query_text: str = "",
) -> tuple[str | None, float]:
    """Look up a cached response whose embedding is similar to ``query_embedding``.

    Returns ``(response, similarity_score)``. ``response`` is None on miss.
    ``similarity_score`` is 0.0 on miss and 1.0 on a perfect match (cosine
    similarity range: 0.0 → 1.0 for non-negative vectors like MiniLM).

    **Two-stage matching:**
    1. Deterministic: years and numbers extracted from ``query_text`` must
       exactly match those from the cached query. This prevents "2020-2023"
       from matching a cached "2015-2020".
    2. Semantic: the remaining text (field names, intent) is matched via
       embedding cosine similarity above ``threshold``.

    The user's namespace is always filtered — there is no way for another
    user's cached entry to leak through.
    """
    if query_embedding is None:
        return None, 0.0
    if not user_id:
        user_id = "anonymous"

    query_years = _extract_years(query_text)
    query_numbers = _extract_numbers(query_text)

    # Try Redis Stack first.
    try:
        r = _get_redis()
        if r is not None and _ensure_redis_index(r):
            qvec = query_embedding.astype(np.float32).tobytes()
            # Fetch top 5 candidates so we can filter by year/number match
            res = r.ft(_REDIS_INDEX).search(
                f"@user_id:{{{user_id}}}",
                vector={"field": "embedding", "vec": qvec, "k": 5},
            )
            if res and res.docs:
                for top in res.docs:
                    distance = float(getattr(top, "vector_distance", 1.0) or 1.0)
                    similarity = 1.0 - distance
                    if similarity < threshold:
                        continue
                    cached_query = getattr(top, "query", None) or ""
                    cached_years = _extract_years(cached_query)
                    cached_numbers = _extract_numbers(cached_query)
                    if not _numbers_match(query_years, cached_years):
                        continue
                    if not _numbers_match(query_numbers, cached_numbers):
                        continue
                    return top.response, similarity
            return None, 0.0
    except Exception as e:
        # RediSearch missing, connection issue, etc. Fall through to SQLite.
        print(f"⚠️ Redis semantic lookup failed, falling back to SQLite: {e}")

    # SQLite fallback: load the user's embeddings and brute-force cosine sim.
    try:
        from sheet_metadata import list_user_embeddings
    except ImportError:
        return None, 0.0

    rows = list_user_embeddings(user_id)
    if not rows:
        return None, 0.0

    q = query_embedding.astype(np.float64)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return None, 0.0

    best_key = None
    best_response = None
    best_score = 0.0
    for cache_key, response, emb, cached_query in rows:
        # Stage 1: deterministic year/number match
        cached_years = _extract_years(cached_query or "")
        cached_numbers = _extract_numbers(cached_query or "")
        if not _numbers_match(query_years, cached_years):
            continue
        if not _numbers_match(query_numbers, cached_numbers):
            continue

        # Stage 2: semantic similarity on the non-numeric text
        try:
            v = np.asarray(emb, dtype=np.float64)
        except (TypeError, ValueError):
            continue
        v_norm = float(np.linalg.norm(v))
        if v_norm == 0.0:
            continue
        score = float(np.dot(q, v) / (q_norm * v_norm))
        if score > best_score:
            best_score = score
            best_key = cache_key
            best_response = response

    if best_score >= threshold and best_response is not None:
        return best_response, best_score
    return None, 0.0


def store_cached(
    user_id: str,
    query: str,
    query_embedding: np.ndarray | None,
    response: str,
    model: str,
    cache_key: str | None = None,
) -> None:
    """Store a query+response+embedding in the cache.

    ``cache_key`` is optional — defaults to the SHA-256 of ``model:query`` so
    the exact-match and semantic cache share the same key space.
    """
    if not user_id:
        user_id = "anonymous"
    if cache_key is None:
        import hashlib
        cache_key = hashlib.sha256(f"{model}:{query}".encode()).hexdigest()

    emb_list: list[float] | None = None
    if query_embedding is not None:
        emb_list = query_embedding.astype(np.float64).tolist()

    # Redis write (best-effort).
    try:
        r = _get_redis()
        if r is not None and _ensure_redis_index(r):
            import hashlib as _hl
            rkey = _redis_key_for_user(user_id, cache_key)
            payload: dict[str, Any] = {
                "user_id": user_id,
                "query": query,
                "model": model,
                "response": response,
            }
            # Store query text for deterministic year/number matching on lookup
            if emb_list is not None:
                payload["embedding"] = np.asarray(emb_list, dtype=np.float32).tobytes()
            r.hset(rkey, mapping=payload)
            r.expire(rkey, DEFAULT_TTL_SECONDS)
    except Exception as e:
        print(f"⚠️ Redis semantic write failed: {e}")

    # SQLite write (always — this is the source of truth, Redis is a cache).
    try:
        from sheet_metadata import set_cached_response
        set_cached_response(model, query, response, user_id=user_id, embedding=emb_list)
    except Exception as e:
        print(f"⚠️ SQLite semantic write failed: {e}")


def invalidate_user_cache(user_id: str) -> int:
    """Delete every cache entry owned by ``user_id``.

    Called when the user uploads a new file or deletes an existing one — any
    cached answer about the old data is stale.

    Returns the total number of entries removed (Redis + SQLite).
    """
    if not user_id:
        user_id = "anonymous"
    deleted = 0

    # Redis: best-effort deletion of every key in the user's namespace.
    try:
        r = _get_redis()
        if r is not None:
            keys = list(r.scan_iter(match=_redis_user_keys(user_id), count=100))
            if keys:
                deleted += r.delete(*keys)
    except Exception as e:
        print(f"⚠️ Redis invalidate failed: {e}")

    # SQLite: the authoritative store.
    try:
        from sheet_metadata import invalidate_user_cache as sqlite_invalidate
        deleted += sqlite_invalidate(user_id)
    except Exception as e:
        print(f"⚠️ SQLite invalidate failed: {e}")

    # Also clear intermediate result cache (retrieve + sandbox + structured keys)
    try:
        from result_cache import invalidate_user_results
        deleted += invalidate_user_results(user_id)
    except Exception as e:
        print(f"⚠️ Result cache invalidate failed: {e}")

    return deleted


def is_available() -> bool:
    """Return True if semantic caching is currently usable.

    Becomes False when sentence-transformers isn't installed or the model
    failed to download. Useful for /cache/stats and the front-end.
    """
    return _get_model() is not None