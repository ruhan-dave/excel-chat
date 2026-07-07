"""
Intermediate result cache for the query pipeline.

Caches individual retrieval and computation results so that subsequent
queries can reuse them without re-scanning DataFrames or re-executing
sandbox code.

Three layers:
  1. Retrieve cache — keyed by field/year/sheet, stores raw tool return strings
  2. Sandbox cache — keyed by SHA-256 of normalized code, stores sandbox output
  3. Post-execution structured keys — derived from QueryPlan + step_results,
     stores computed values under canonical keys like ``sum_revenue_2022_2023``

All layers are per-user (scoped by ``user_id``) and share a 7-day TTL.
Redis is the primary store when ``REDIS_URL`` is set; SQLite is the fallback.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# Redis client (shared singleton from cache_service.py)
# ---------------------------------------------------------------------------

from cache_service import _get_redis, reset_redis_client, DEFAULT_TTL_SECONDS


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_field(name: str) -> str:
    """Normalize a field name for cache key construction.

    ``"Wages and salaries"`` → ``"wages_and_salaries"``
    """
    s = name.lower().strip()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def normalize_year(year: str) -> str:
    """Normalize a year string. Preserves format (FY2022, 2022-2023)."""
    return year.strip()


def normalize_sheet(name: str) -> str:
    """Normalize a sheet name for cache key construction."""
    s = name.lower().strip()
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


# Code name → cache canonical name mapping
OP_NAME_MAP: dict[str, str] = {
    "add": "sum",
    "divide": "division",
    "multiply": "multiply",
    "subtract": "subtract",
    "ratio": "ratio",
    "return_percentage": "return_percentage",
    "yoy_growth": "yoy_growth",
    "percentage_change": "percentage_change",
    "cagr": "cagr",
    "average": "average",
    "median": "median",
    "max": "max",
    "min": "min",
    "stdev": "stdev",
    "sqrt": "sqrt",
    "power": "power",
    "log": "log",
    "abs": "abs",
    "negate": "negate",
    "exp": "exp",
}

# Reverse map: user-facing alias → canonical
ALIAS_TO_CANONICAL: dict[str, str] = {
    "total": "sum",
    "summation": "sum",
    "grand_total": "sum",
    "add_up": "sum",
    "aggregate": "sum",
    "divided": "division",
    "per": "division",
    "multiplication": "multiply",
    "times": "multiply",
    "product": "multiply",
    "difference": "subtract",
    "minus": "subtract",
    "less": "subtract",
    "ratio_of": "ratio",
    "percentage": "return_percentage",
    "percent": "return_percentage",
    "as_percent_of": "return_percentage",
    "year_over_year": "yoy_growth",
    "annual_growth": "yoy_growth",
    "growth_rate": "yoy_growth",
    "pct_change": "percentage_change",
    "relative_change": "percentage_change",
    "compound_annual_growth": "cagr",
    "compound_growth": "cagr",
    "mean": "average",
    "avg": "average",
    "middle_value": "median",
    "maximum": "max",
    "highest": "max",
    "peak": "max",
    "minimum": "min",
    "lowest": "min",
    "bottom": "min",
    "standard_deviation": "stdev",
    "std_dev": "stdev",
    "square_root": "sqrt",
    "exponent": "power",
    "squared": "power",
    "cubed": "power",
    "logarithm": "log",
    "ln": "log",
    "absolute": "abs",
    "absolute_value": "abs",
    "negative": "negate",
    "opposite": "negate",
    "exponential": "exp",
}


def normalize_operation(name: str) -> str:
    """Map a code name or user alias to the canonical cache name.

    >>> normalize_operation("add")
    'sum'
    >>> normalize_operation("total")
    'sum'
    >>> normalize_operation("divide")
    'division'
    """
    key = name.lower().strip()
    if key in OP_NAME_MAP:
        return OP_NAME_MAP[key]
    if key in ALIAS_TO_CANONICAL:
        return ALIAS_TO_CANONICAL[key]
    return key


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------

def build_retrieve_key(field: str, year: str, sheet: str = "") -> str:
    """Build a structured cache key for a retrieve operation.

    Unscoped: ``revenue_2022``
    Scoped:   ``sheetA.revenue_2022``
    """
    f = normalize_field(field)
    y = normalize_year(year)
    if sheet:
        s = normalize_sheet(sheet)
        return f"{s}.{f}_{y}"
    return f"{f}_{y}"


def build_step_key(
    step_action: str,
    step_args: list[str],
    all_steps: dict[str, Any] | None = None,
) -> str | None:
    """Build a canonical structured key from a plan step.

    Returns None if the key cannot be derived (e.g., compute steps,
    or named operations referencing non-retrieve steps).

    ``all_steps`` is the plan dict: ``{"step1": PlanStep, ...}``.
    """
    if step_action == "retrieve":
        if len(step_args) >= 3:
            return build_retrieve_key(step_args[1], step_args[2], step_args[0])
        elif len(step_args) >= 2:
            return build_retrieve_key(step_args[0], step_args[1])
        return None

    # Named operations
    canonical_op = OP_NAME_MAP.get(step_action)
    if canonical_op is None:
        # Check if it's already a canonical name or alias
        canonical_op = normalize_operation(step_action)
        if canonical_op == step_action.lower().strip():
            return None  # unknown operation

    if not all_steps:
        return None

    resolved: list[str] = []
    for arg in step_args:
        arg_str = str(arg).strip()
        if arg_str.startswith("step"):
            ref_step = all_steps.get(arg_str)
            if ref_step is None:
                return None
            ref_action = getattr(ref_step, "action", "") or ref_step.get("action", "")
            ref_args = getattr(ref_step, "args", []) or ref_step.get("args", [])
            if ref_action == "retrieve":
                ref_key = build_step_key(ref_action, list(ref_args), all_steps)
                if ref_key is None:
                    return None
                resolved.append(ref_key)
            else:
                return None  # can't derive key for non-retrieve refs
        else:
            resolved.append(f"lit_{arg_str}")

    return f"{canonical_op}_{'_'.join(resolved)}"


# ---------------------------------------------------------------------------
# Code normalization for sandbox cache
# ---------------------------------------------------------------------------

def normalize_code(code: str) -> str:
    """Normalize Python code before hashing to improve cache hit rate.

    Strips comments, normalizes whitespace, removes blank lines.
    """
    lines = []
    for line in code.split("\n"):
        # Strip inline comments
        if "#" in line:
            line = line[:line.index("#")]
        line = line.rstrip()
        if line.strip():
            lines.append(line)
    return "\n".join(lines)


def code_hash(code: str) -> str:
    """SHA-256 of normalized code."""
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Layer 1: Retrieve cache
# ---------------------------------------------------------------------------

_RESULT_PREFIX = "result:"
_SANDBOX_PREFIX = "sandbox:"


def result_cache_get(user_id: str, key: str) -> str | None:
    """Get a cached result by structured key."""
    if not user_id:
        user_id = "anonymous"
    redis_key = f"{_RESULT_PREFIX}{user_id}:{key}"
    try:
        r = _get_redis()
        if r is not None:
            val = r.get(redis_key)
            if val is not None:
                return val
            # Redis miss — fall through to SQLite (may have data from dual-write)
    except Exception as e:
        print(f"⚠️ Redis result_cache_get failed: {e}")
        reset_redis_client()

    # SQLite fallback / source of truth.
    try:
        from sheet_metadata import result_cache_get as sqlite_get
        return sqlite_get(user_id, key)
    except Exception:
        return None


def result_cache_set(
    user_id: str,
    key: str,
    value: str,
    cache_type: str = "retrieve",
    description: str | None = None,
    structured_key: str | None = None,
    embedding: list[float] | None = None,
) -> None:
    """Store a result in the cache.

    Dual-write: writes to Redis (when available) AND SQLite (source of truth).
    """
    if not user_id:
        user_id = "anonymous"
    redis_key = f"{_RESULT_PREFIX}{user_id}:{key}"
    redis_ok = False
    try:
        r = _get_redis()
        if r is not None:
            r.setex(redis_key, DEFAULT_TTL_SECONDS, value)
            redis_ok = True
    except Exception as e:
        print(f"⚠️ Redis result_cache_set failed: {e}")
        reset_redis_client()

    # SQLite write — always (source of truth).
    try:
        from sheet_metadata import result_cache_set as sqlite_set
        sqlite_set(user_id, key, value, cache_type, description, structured_key, embedding)
    except Exception as e:
        if not redis_ok:
            print(f"⚠️ Both Redis and SQLite result_cache_set failed: {e}")
        else:
            print(f"⚠️ SQLite result_cache_set failed (Redis write succeeded): {e}")


# ---------------------------------------------------------------------------
# Layer 2: Sandbox cache
# ---------------------------------------------------------------------------

def sandbox_cache_get(user_id: str, code: str) -> str | None:
    """Get a cached sandbox execution result."""
    if not user_id:
        user_id = "anonymous"
    ch = code_hash(code)
    redis_key = f"{_SANDBOX_PREFIX}{user_id}:{ch}"
    try:
        r = _get_redis()
        if r is not None:
            val = r.get(redis_key)
            if val is not None:
                return val
            # Redis miss — fall through to SQLite
    except Exception as e:
        print(f"⚠️ Redis sandbox_cache_get failed: {e}")
        reset_redis_client()

    # SQLite fallback / source of truth.
    try:
        from sheet_metadata import result_cache_get as sqlite_get
        return sqlite_get(user_id, ch)
    except Exception:
        return None


def sandbox_cache_set(user_id: str, code: str, value: str) -> None:
    """Store a sandbox execution result.

    Dual-write: writes to Redis (when available) AND SQLite (source of truth).
    """
    if not user_id:
        user_id = "anonymous"
    ch = code_hash(code)
    redis_key = f"{_SANDBOX_PREFIX}{user_id}:{ch}"
    redis_ok = False
    try:
        r = _get_redis()
        if r is not None:
            r.setex(redis_key, DEFAULT_TTL_SECONDS, value)
            redis_ok = True
    except Exception as e:
        print(f"⚠️ Redis sandbox_cache_set failed: {e}")
        reset_redis_client()

    # SQLite write — always (source of truth).
    try:
        from sheet_metadata import result_cache_set as sqlite_set
        sqlite_set(user_id, ch, value, "sandbox")
    except Exception as e:
        if not redis_ok:
            print(f"⚠️ Both Redis and SQLite sandbox_cache_set failed: {e}")
        else:
            print(f"⚠️ SQLite sandbox_cache_set failed (Redis write succeeded): {e}")


# ---------------------------------------------------------------------------
# Layer 3: Post-execution structured key derivation
# ---------------------------------------------------------------------------

def cache_step_results(
    user_id: str,
    plan: Any,
    execution_result: Any,
) -> None:
    """After executor returns, store each step result under its canonical key.

    ``plan`` is a QueryPlan (Pydantic model). ``execution_result`` is an
    ExecutionResult. Both are from pipeline.py.
    """
    if not user_id:
        user_id = "anonymous"

    step_results = getattr(execution_result, "step_results", None) or {}
    plan_dict = getattr(plan, "plan", None)

    if plan_dict:
        for step_name, step in plan_dict.items():
            value = step_results.get(step_name)
            if value is None:
                continue
            step_action = getattr(step, "action", "")
            step_args = list(getattr(step, "args", []))

            structured_key = build_step_key(step_action, step_args, plan_dict)
            if structured_key is not None:
                result_cache_set(
                    user_id, structured_key, str(value),
                    cache_type="structured",
                )

            # For compute steps, also store via semantic alias
            if step_action == "compute" and step_args:
                description = step_args[0]
                try:
                    from semantic_cache import embed_query
                    emb = embed_query(description)
                    if emb is not None:
                        emb_list = emb.tolist()
                        desc_hash = hashlib.sha256(description.encode()).hexdigest()
                        result_cache_set(
                            user_id, desc_hash, str(value),
                            cache_type="semantic_step",
                            description=description,
                            structured_key=structured_key,
                            embedding=emb_list,
                        )
                except Exception:
                    pass

    # Handle retrieve_numbers task type (items list, no plan steps)
    items = getattr(plan, "items", None)
    if items and not plan_dict:
        final = getattr(execution_result, "final_answer", None)
        if final is not None:
            for item in items:
                parts = [p.strip() for p in item.split(",")]
                if len(parts) == 2:
                    key = build_retrieve_key(parts[0], parts[1])
                    result_cache_set(user_id, key, str(final), cache_type="retrieve")
                elif len(parts) == 3:
                    key = build_retrieve_key(parts[1], parts[2], parts[0])
                    result_cache_set(user_id, key, str(final), cache_type="retrieve")


# ---------------------------------------------------------------------------
# Semantic alias lookup for compute steps
# ---------------------------------------------------------------------------

def find_similar_step(
    user_id: str,
    description_embedding: Any,
    threshold: float = 0.90,
) -> dict[str, Any] | None:
    """Find a cached compute step with a similar description.

    Returns ``{"value": ..., "structured_key": ..., "similarity": ...}`` or None.
    """
    if description_embedding is None:
        return None
    if not user_id:
        user_id = "anonymous"

    # SQLite fallback (primary for semantic step search — no Redis index yet)
    try:
        import numpy as np
        from sheet_metadata import list_user_result_embeddings

        rows = list_user_result_embeddings(user_id, "semantic_step")
        if not rows:
            return None

        q = np.asarray(description_embedding, dtype=np.float64)
        q_norm = float(np.linalg.norm(q))
        if q_norm == 0.0:
            return None

        best: dict[str, Any] | None = None
        best_score = 0.0
        for cache_key, value, emb, structured_key in rows:
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
                best = {
                    "value": value,
                    "structured_key": structured_key,
                    "similarity": score,
                }

        if best is not None and best_score >= threshold:
            return best
    except Exception as e:
        print(f"⚠️ find_similar_step failed: {e}")

    return None


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

def invalidate_user_results(user_id: str) -> int:
    """Delete all cached results for a user across all layers.

    Called on file upload/delete — all cached retrievals and
    computations are stale.
    """
    if not user_id:
        user_id = "anonymous"
    deleted = 0

    # Redis: scan and delete result:* and sandbox:* keys
    try:
        r = _get_redis()
        if r is not None:
            for pattern in [
                f"{_RESULT_PREFIX}{user_id}:*",
                f"{_SANDBOX_PREFIX}{user_id}:*",
            ]:
                keys = list(r.scan_iter(match=pattern, count=100))
                if keys:
                    deleted += r.delete(*keys)
    except Exception as e:
        print(f"⚠️ Redis invalidate_user_results failed: {e}")

    # SQLite
    try:
        from sheet_metadata import invalidate_user_result_cache
        deleted += invalidate_user_result_cache(user_id)
    except Exception as e:
        print(f"⚠️ SQLite invalidate_user_results failed: {e}")

    return deleted
