"""
Tests for the per-user semantic cache (semantic_cache.py + sheet_metadata).

Coverage:
  * embed_query returns a 384-dim L2-normalized vector (or None when the
    sentence-transformers model isn't available; we use a fake in tests).
  * find_similar_cached returns the cached response for paraphrased queries
    above the threshold (cosine similarity > 0.92).
  * find_similar_cached returns None for unrelated queries.
  * Per-user isolation: user A's cache is never returned for user B.
  * Cache invalidation: invalidate_user_cache removes every entry for a user.

The tests use the SQLite fallback path (no Redis) since that's what runs in
CI. The Redis code path is exercised by manual integration testing against
Redis Stack.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

BACKEND_SRC = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _maybe_await(value):
    if inspect.iscoroutine(value):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(value)
        finally:
            loop.close()
    return value


@pytest.fixture()
def temp_sheet_db(monkeypatch, tmp_path):
    """Redirect sheet_metadata at a fresh SQLite DB."""
    db_path = tmp_path / "test_semantic.db"
    import sheet_metadata
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()
    yield str(db_path)


@pytest.fixture()
def fake_model(monkeypatch):
    """Replace the SentenceTransformer with a deterministic fake.

    Two semantically-related queries produce near-identical vectors;
    unrelated queries produce orthogonal vectors. This lets us test
    cosine-similarity behavior without loading the 80 MB real model.
    """
    import semantic_cache

    # Curated vocabulary: every fake query is a unit vector that points in
    # one of a handful of directions. Paraphrases share a direction;
    # unrelated topics get distinct directions.
    VOCAB = {
        # revenue direction
        "revenue in 2022": np.array([1.0, 0.0, 0.0, 0.0]),
        "2022 revenue": np.array([0.95, 0.05, 0.0, 0.0]),  # near-paraphrase
        "show me revenue for fy2022": np.array([0.97, 0.0, 0.03, 0.0]),
        "what was the revenue": np.array([0.99, 0.0, 0.0, 0.01]),
        "revenue in 2023": np.array([0.98, 0.0, 0.0, 0.02]),  # same direction, diff year
        "revenue in 2021": np.array([0.97, 0.01, 0.0, 0.02]),  # same direction, diff year
        # employees direction
        "employee count": np.array([0.0, 0.0, 1.0, 0.0]),
        "how many employees": np.array([0.05, 0.0, 0.97, 0.0]),
        # expenses direction
        "expenses last year": np.array([0.0, 1.0, 0.0, 0.0]),
        "spending breakdown": np.array([0.0, 0.95, 0.0, 0.05]),
        # orthogonal direction (totally unrelated)
        "office address": np.array([0.0, 0.0, 0.0, 1.0]),
    }

    def fake_encode(text, normalize_embeddings=True, convert_to_numpy=True):
        text_lower = text.strip().lower()
        if text_lower not in VOCAB:
            # Hash the string into a deterministic pseudo-random vector.
            # This keeps unknown queries deterministic across calls so two
            # unknown queries that happen to hash similarly still differ.
            seed = abs(hash(text_lower)) % (2 ** 32)
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(128)
            v = v / np.linalg.norm(v)
        else:
            v = VOCAB[text_lower]
        if normalize_embeddings:
            n = np.linalg.norm(v)
            if n > 0:
                v = v / n
        return v.astype(np.float64)

    # Reset BEFORE monkeypatch so we start clean, then inject the fake.
    semantic_cache.reset_model()
    fake = MagicMock()
    fake.encode.side_effect = fake_encode
    monkeypatch.setattr(semantic_cache, "_model", fake)
    monkeypatch.setattr(semantic_cache, "_model_load_failed", False)
    yield fake


# ---------------------------------------------------------------------------
# embed_query
# ---------------------------------------------------------------------------


def test_embed_query_returns_normalized_vector(temp_sheet_db, fake_model):
    """embed_query returns a 1024-dim (or any-dim, normalized) vector."""
    import semantic_cache
    vec = semantic_cache.embed_query("revenue in 2022")
    assert vec is not None
    assert isinstance(vec, np.ndarray)
    # Our fake uses 4-dim vectors. The real model is 384-dim; we don't assert
    # the exact size here because the fake is intentionally tiny. We DO assert
    # that the vector is L2-normalized (norm ≈ 1.0).
    norm = float(np.linalg.norm(vec))
    assert math.isclose(norm, 1.0, abs_tol=1e-6)


def test_embed_query_returns_none_when_model_unavailable(temp_sheet_db, monkeypatch):
    """If the model isn't installed, embed_query returns None (not crash)."""
    import semantic_cache
    monkeypatch.setattr(semantic_cache, "_model_load_failed", True)
    monkeypatch.setattr(semantic_cache, "_model", None)
    assert semantic_cache.embed_query("anything") is None


# ---------------------------------------------------------------------------
# find_similar_cached: paraphrases hit, unrelated queries miss
# ---------------------------------------------------------------------------


def test_find_similar_cached_matches_paraphrased_query(temp_sheet_db, fake_model):
    """'Revenue in 2022' and '2022 revenue' must hit the same cache entry."""
    import semantic_cache

    user_id = "alice"
    response = "Revenue was $4.2M in 2022."
    emb = semantic_cache.embed_query("revenue in 2022")
    semantic_cache.store_cached(
        user_id=user_id,
        query="revenue in 2022",
        query_embedding=emb,
        response=response,
        model="test-model",
    )

    # Now query with a paraphrase.
    paraphrase_emb = semantic_cache.embed_query("2022 revenue")
    cached, score = semantic_cache.find_similar_cached(
        user_id, paraphrase_emb, threshold=0.92
    )

    assert cached == response
    assert score >= 0.92


def test_find_similar_cached_returns_none_for_unrelated_query(
    temp_sheet_db, fake_model
):
    """'Employee count' should NOT match a cache of 'Revenue in 2022'."""
    import semantic_cache

    user_id = "alice"
    response = "Revenue was $4.2M in 2022."
    emb = semantic_cache.embed_query("revenue in 2022")
    semantic_cache.store_cached(
        user_id=user_id,
        query="revenue in 2022",
        query_embedding=emb,
        response=response,
        model="test-model",
    )

    unrelated_emb = semantic_cache.embed_query("employee count")
    cached, score = semantic_cache.find_similar_cached(
        user_id, unrelated_emb, threshold=0.92
    )

    assert cached is None
    assert score < 0.92


def test_find_similar_cached_rejects_different_years(temp_sheet_db, fake_model):
    """A query for 2022 revenue must NOT hit a cache of 2023 revenue even
    if the embedding similarity is high — the years differ deterministically."""
    import semantic_cache

    user_id = "alice"
    cached_query = "revenue in 2022"
    response = "Revenue was $4.2M in 2022."
    emb = semantic_cache.embed_query(cached_query)
    semantic_cache.store_cached(
        user_id=user_id,
        query=cached_query,
        query_embedding=emb,
        response=response,
        model="test-model",
    )

    # Paraphrase with a DIFFERENT year — should NOT hit cache.
    new_query = "revenue in 2023"
    new_emb = semantic_cache.embed_query(new_query)
    cached, score = semantic_cache.find_similar_cached(
        user_id, new_emb, threshold=0.88, query_text=new_query
    )

    assert cached is None, f"Should not hit cache for different years (score={score:.3f})"


def test_find_similar_cached_matches_same_years_paraphrase(temp_sheet_db, fake_model):
    """'revenue in 2022' and '2022 revenue' must hit cache — same years."""
    import semantic_cache

    user_id = "alice"
    cached_query = "revenue in 2022"
    response = "Revenue was $4.2M in 2022."
    emb = semantic_cache.embed_query(cached_query)
    semantic_cache.store_cached(
        user_id=user_id,
        query=cached_query,
        query_embedding=emb,
        response=response,
        model="test-model",
    )

    # Paraphrase with same year — should hit cache.
    new_query = "2022 revenue"
    new_emb = semantic_cache.embed_query(new_query)
    cached, score = semantic_cache.find_similar_cached(
        user_id, new_emb, threshold=0.88, query_text=new_query
    )

    assert cached == response, f"Should hit cache for same years (score={score:.3f})"


# ---------------------------------------------------------------------------
# Per-user isolation
# ---------------------------------------------------------------------------


def test_user_isolation_user_b_does_not_see_user_a_cache(
    temp_sheet_db, fake_model
):
    """User B's query must NEVER return User A's cached response."""
    import semantic_cache

    alice_response = "Alice's revenue was $1M."
    bob_response = "Bob's revenue was $5M."

    # Alice stores her revenue cache.
    alice_emb = semantic_cache.embed_query("revenue in 2022")
    semantic_cache.store_cached(
        user_id="alice",
        query="revenue in 2022",
        query_embedding=alice_emb,
        response=alice_response,
        model="test-model",
    )

    # Bob queries with an identical-or-near paraphrase.
    bob_emb = semantic_cache.embed_query("revenue in 2022")
    cached, score = semantic_cache.find_similar_cached(
        "bob", bob_emb, threshold=0.92
    )

    # Bob must NOT get Alice's response back.
    assert cached is None
    assert cached != alice_response

    # Bob's own cache should also be empty.
    assert cached != bob_response

    # Sanity check: Alice's own paraphrase still hits her cache.
    alice_paraphrase = semantic_cache.embed_query("2022 revenue")
    a_cached, a_score = semantic_cache.find_similar_cached(
        "alice", alice_paraphrase, threshold=0.92
    )
    assert a_cached == alice_response


def test_user_isolation_invalidate_only_affects_target_user(
    temp_sheet_db, fake_model
):
    """invalidate_user_cache('alice') must not touch Bob's cache."""
    import semantic_cache

    alice_emb = semantic_cache.embed_query("revenue in 2022")
    bob_emb = semantic_cache.embed_query("expenses last year")

    semantic_cache.store_cached(
        user_id="alice", query="revenue in 2022", query_embedding=alice_emb,
        response="A-r", model="m",
    )
    semantic_cache.store_cached(
        user_id="bob", query="expenses last year", query_embedding=bob_emb,
        response="B-r", model="m",
    )

    deleted = semantic_cache.invalidate_user_cache("alice")
    assert deleted == 1

    # Alice's cache is gone.
    a_cached, _ = semantic_cache.find_similar_cached(
        "alice", alice_emb, threshold=0.92
    )
    assert a_cached is None

    # Bob's cache survives.
    b_cached, _ = semantic_cache.find_similar_cached(
        "bob", bob_emb, threshold=0.92
    )
    assert b_cached == "B-r"


# ---------------------------------------------------------------------------
# Cache invalidation on file upload (wiring into main.py)
# ---------------------------------------------------------------------------


def test_upload_endpoint_invalidates_user_semantic_cache(
    monkeypatch, tmp_path, fake_model
):
    """POST /upload/ must invalidate the requesting user's semantic cache.

    This is the end-to-end check that the ``semantic_invalidate_user_cache``
    call in main.py fires when a file is uploaded. We stub out the heavy
    parts (S3 upload, Excel parsing) and only assert that the cache is
    cleared.
    """
    # Re-route sheet_metadata and load main.py.
    import importlib
    import sheet_metadata
    db_path = tmp_path / "test_upload_inv.db"
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()

    if "main" in sys.modules:
        del sys.modules["main"]
    importlib.reload(sheet_metadata)
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()
    import semantic_cache
    main = importlib.import_module("main")

    # Seed a cache entry for user "alice".
    alice_emb = semantic_cache.embed_query("revenue in 2022")
    semantic_cache.store_cached(
        user_id="alice", query="revenue in 2022", query_embedding=alice_emb,
        response="cached-revenue", model="m",
    )
    # Confirm it's reachable.
    cached, _ = semantic_cache.find_similar_cached(
        "alice", alice_emb, threshold=0.92
    )
    assert cached == "cached-revenue"

    # Stub the upload pipeline so we don't hit S3 or pandas.
    from fastapi import UploadFile
    import excelservices
    import guardrails

    async def _noop(*args, **kwargs):
        return None

    fake_upload = MagicMock(spec=UploadFile)
    fake_upload.filename = "test.xlsx"
    async def _fake_read(*args, **kwargs):
        return b""
    fake_upload.read = _fake_read

    class _FakeService:
        @staticmethod
        def load_sheet_metadata_from_file(*a, **k):
            return []
        @staticmethod
        def detect_schema_groups(*a, **k):
            return None
        @staticmethod
        def auto_describe_all_sheets(*a, **k):
            return None
        @staticmethod
        def load_all_sheets(*a, **k):
            return {}

    # Patch ExcelService at the source module (create_upload_file does a
    # local `from excelservices import ExcelService as _ES`).
    monkeypatch.setattr(excelservices, "ExcelService", _FakeService)
    monkeypatch.setattr(main, "ExcelService", _FakeService)
    monkeypatch.setattr(main, "upload_to_s3", lambda *a, **k: None)
    monkeypatch.setattr(main, "save_file", lambda *a, **k: None)
    monkeypatch.setattr(main, "save_sheet", lambda *a, **k: None)

    # Bypass guardrail checks for the fake empty file.
    from guardrails import GuardrailResult
    _ok = GuardrailResult(allowed=True, reason="", message="", category="")
    monkeypatch.setattr(guardrails, "validate_file_upload", lambda *a, **k: _ok)
    monkeypatch.setattr(main, "validate_file_upload", lambda *a, **k: _ok)
    monkeypatch.setattr(
        main, "detect_sensitive_data_in_dataframe",
        lambda df: (False, []),
    )

    # Run the endpoint as an async function.
    result = _maybe_await(
        main.create_upload_file(fake_upload, x_user_id="alice")
    )

    # After upload, Alice's cache must be empty.
    cached_after, _ = semantic_cache.find_similar_cached(
        "alice", alice_emb, threshold=0.92
    )
    assert cached_after is None
    assert result["user_id"] == "alice"


# ---------------------------------------------------------------------------
# SQLite fallback returns vectors with correct dimensionality
# ---------------------------------------------------------------------------


def test_sqlite_fallback_round_trip(temp_sheet_db, fake_model):
    """Storing and retrieving an embedding via SQLite preserves dimensions."""
    import semantic_cache
    emb = semantic_cache.embed_query("revenue in 2022")
    semantic_cache.store_cached(
        user_id="u1", query="revenue in 2022", query_embedding=emb,
        response="ok", model="m",
    )

    from sheet_metadata import list_user_embeddings
    rows = list_user_embeddings("u1")
    assert len(rows) == 1
    cache_key, response, stored_emb, query_text = rows[0]
    assert response == "ok"
    # The stored embedding must be a list of floats with the same dim as the
    # original vector.
    assert isinstance(stored_emb, list)
    assert len(stored_emb) == emb.shape[0]


# ---------------------------------------------------------------------------
# is_available helper
# ---------------------------------------------------------------------------


def test_is_available_true_when_model_loaded(temp_sheet_db, fake_model):
    import semantic_cache
    assert semantic_cache.is_available() is True


def test_is_available_false_when_model_load_failed(
    temp_sheet_db, monkeypatch
):
    import semantic_cache
    monkeypatch.setattr(semantic_cache, "_model_load_failed", True)
    monkeypatch.setattr(semantic_cache, "_model", None)
    assert semantic_cache.is_available() is False


# ---------------------------------------------------------------------------
# Main module loads without sentence-transformers installed
# ---------------------------------------------------------------------------


def test_main_loads_without_semantic_model(monkeypatch, tmp_path):
    """Importing main.py must not crash even if the embedding model fails to load."""
    import importlib
    import sheet_metadata
    db_path = tmp_path / "test_main_load.db"
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    if "main" in sys.modules:
        del sys.modules["main"]
    importlib.reload(sheet_metadata)
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()

    # Force embed_query to return None (simulating model load failure).
    import semantic_cache
    monkeypatch.setattr(semantic_cache, "_model_load_failed", True)
    monkeypatch.setattr(semantic_cache, "_model", None)

    main = importlib.import_module("main")
    # Query endpoint exists and is callable.
    assert callable(main.query_rag)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))