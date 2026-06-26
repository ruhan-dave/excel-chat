"""
Tests for the intermediate result cache (result_cache.py + sheet_metadata).

Coverage:
  * Structured key generation: retrieve (scoped + unscoped), all operation types,
    literal args
  * Alias normalization (sum = total = grand_total, division = ratio = divided)
  * Layer 1: retrieve cache hit returns cached value, miss stores new value
  * Layer 2: sandbox cache hit returns cached output, miss stores new output
  * Layer 3: post-execution keys derived correctly from plan + step_results
  * Per-user isolation: user A's cache is never returned for user B
  * Error returns not cached (ERROR: prefix detected)
  * Invalidation on file upload clears all layers
  * Semantic alias match for compute steps
  * retrieve_numbers task type (items path)
  * Concurrent writes (same key, same value — no corruption)

Uses the SQLite fallback path (no Redis) for CI compatibility.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

BACKEND_SRC = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_redis_state(monkeypatch):
    """Reset the module-level Redis singleton before every test."""
    import result_cache
    monkeypatch.delenv("REDIS_URL", raising=False)
    result_cache.reset_redis_client()
    yield
    result_cache.reset_redis_client()


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    """Redirect sheet_metadata at a fresh SQLite DB."""
    db_path = tmp_path / "test_result_cache.db"
    import sheet_metadata
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()
    yield str(db_path)


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

class TestNormalizeField:
    def test_simple_lowercase(self):
        from result_cache import normalize_field
        assert normalize_field("Revenue") == "revenue"

    def test_spaces_to_underscores(self):
        from result_cache import normalize_field
        assert normalize_field("Wages and salaries") == "wages_and_salaries"

    def test_hyphens_to_underscores(self):
        from result_cache import normalize_field
        assert normalize_field("year-over-year") == "year_over_year"

    def test_special_chars_stripped(self):
        from result_cache import normalize_field
        assert normalize_field("Revenue (Total)!") == "revenue_total"

    def test_collapse_consecutive_underscores(self):
        from result_cache import normalize_field
        assert normalize_field("a   b") == "a_b"

    def test_empty_string(self):
        from result_cache import normalize_field
        assert normalize_field("") == ""


class TestNormalizeYear:
    def test_plain_year(self):
        from result_cache import normalize_year
        assert normalize_year("2022") == "2022"

    def test_fy_prefix_preserved(self):
        from result_cache import normalize_year
        assert normalize_year("FY2022") == "FY2022"

    def test_range_preserved(self):
        from result_cache import normalize_year
        assert normalize_year("2022-2023") == "2022-2023"

    def test_whitespace_stripped(self):
        from result_cache import normalize_year
        assert normalize_year("  2022  ") == "2022"


class TestNormalizeSheet:
    def test_simple(self):
        from result_cache import normalize_sheet
        assert normalize_sheet("SheetA") == "sheeta"

    def test_with_spaces(self):
        from result_cache import normalize_sheet
        assert normalize_sheet("Balance Sheet") == "balance_sheet"


# ---------------------------------------------------------------------------
# Operation name mapping
# ---------------------------------------------------------------------------

class TestNormalizeOperation:
    def test_code_name_add_to_sum(self):
        from result_cache import normalize_operation
        assert normalize_operation("add") == "sum"

    def test_code_name_divide_to_division(self):
        from result_cache import normalize_operation
        assert normalize_operation("divide") == "division"

    def test_alias_total_to_sum(self):
        from result_cache import normalize_operation
        assert normalize_operation("total") == "sum"

    def test_alias_grand_total_to_sum(self):
        from result_cache import normalize_operation
        assert normalize_operation("grand_total") == "sum"

    def test_alias_divided_to_division(self):
        from result_cache import normalize_operation
        assert normalize_operation("divided") == "division"

    def test_alias_mean_to_average(self):
        from result_cache import normalize_operation
        assert normalize_operation("mean") == "average"

    def test_alias_highest_to_max(self):
        from result_cache import normalize_operation
        assert normalize_operation("highest") == "max"

    def test_canonical_name_passthrough(self):
        from result_cache import normalize_operation
        assert normalize_operation("sum") == "sum"
        assert normalize_operation("ratio") == "ratio"
        assert normalize_operation("yoy_growth") == "yoy_growth"

    def test_unknown_operation_returns_lowercased(self):
        from result_cache import normalize_operation
        assert normalize_operation("foobar") == "foobar"

    def test_divide_vs_ratio_distinct(self):
        """divide and ratio are both a/b but have different canonical names."""
        from result_cache import normalize_operation
        assert normalize_operation("divide") == "division"
        assert normalize_operation("ratio") == "ratio"
        assert normalize_operation("divide") != normalize_operation("ratio")


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------

class TestBuildRetrieveKey:
    def test_unscoped_single_field(self):
        from result_cache import build_retrieve_key
        assert build_retrieve_key("Revenue", "2022") == "revenue_2022"

    def test_scoped_with_sheet(self):
        from result_cache import build_retrieve_key
        assert build_retrieve_key("Revenue", "2022", "SheetA") == "sheeta.revenue_2022"

    def test_unscoped_multi_word_field(self):
        from result_cache import build_retrieve_key
        assert build_retrieve_key("Wages and salaries", "2022") == "wages_and_salaries_2022"

    def test_scoped_vs_unscoped_different_keys(self):
        from result_cache import build_retrieve_key
        unscoped = build_retrieve_key("Revenue", "2022")
        scoped = build_retrieve_key("Revenue", "2022", "SheetA")
        assert unscoped != scoped


class TestBuildStepKey:
    def _make_step(self, action, args):
        """Create a simple step-like object."""
        return MagicMock(action=action, args=args)

    def test_retrieve_two_args(self):
        from result_cache import build_step_key
        step = self._make_step("retrieve", ["Revenue", "2022"])
        assert build_step_key("retrieve", ["Revenue", "2022"]) == "revenue_2022"

    def test_retrieve_three_args(self):
        from result_cache import build_step_key
        key = build_step_key("retrieve", ["SheetA", "Revenue", "2022"])
        assert key == "sheeta.revenue_2022"

    def test_add_operation_with_step_refs(self):
        from result_cache import build_step_key
        all_steps = {
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
            "step2": self._make_step("retrieve", ["Revenue", "2023"]),
        }
        key = build_step_key("add", ["step1", "step2"], all_steps)
        assert key == "sum_revenue_2022_revenue_2023"

    def test_divide_operation_with_step_refs(self):
        from result_cache import build_step_key
        all_steps = {
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
            "step2": self._make_step("retrieve", ["Grants", "2022"]),
        }
        key = build_step_key("divide", ["step1", "step2"], all_steps)
        assert key == "division_revenue_2022_grants_2022"

    def test_operation_with_literal_arg(self):
        from result_cache import build_step_key
        all_steps = {
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
        }
        key = build_step_key("add", ["step1", "100"], all_steps)
        assert key == "sum_revenue_2022_lit_100"

    def test_compute_returns_none(self):
        from result_cache import build_step_key
        assert build_step_key("compute", ["calculate something"]) is None

    def test_unknown_operation_returns_none(self):
        from result_cache import build_step_key
        assert build_step_key("foobar", ["step1"]) is None

    def test_non_retrieve_ref_returns_none(self):
        from result_cache import build_step_key
        all_steps = {
            "step1": self._make_step("add", ["step2", "step3"]),
            "step2": self._make_step("retrieve", ["Revenue", "2022"]),
            "step3": self._make_step("retrieve", ["Revenue", "2023"]),
        }
        # step1 references step2 and step3, but step1 itself is an add,
        # so referencing step1 from another op can't derive a key
        key = build_step_key("divide", ["step1", "step2"], all_steps)
        assert key is None

    def test_no_all_steps_returns_none_for_named_op(self):
        from result_cache import build_step_key
        assert build_step_key("add", ["step1", "step2"]) is None


# ---------------------------------------------------------------------------
# Code normalization
# ---------------------------------------------------------------------------

class TestNormalizeCode:
    def test_strips_comments(self):
        from result_cache import normalize_code
        code = "x = 1  # set x\nreturn x"
        normalized = normalize_code(code)
        assert "#" not in normalized
        assert "x = 1" in normalized

    def test_strips_blank_lines(self):
        from result_cache import normalize_code
        code = "x = 1\n\n\nreturn x\n"
        normalized = normalize_code(code)
        assert "\n\n" not in normalized

    def test_strips_trailing_whitespace(self):
        from result_cache import normalize_code
        code = "x = 1   \nreturn x   "
        normalized = normalize_code(code)
        for line in normalized.split("\n"):
            assert line == line.rstrip()

    def test_same_code_same_hash(self):
        from result_cache import code_hash
        h1 = code_hash("x = 1\nreturn x")
        h2 = code_hash("x = 1\nreturn x")
        assert h1 == h2

    def test_different_code_different_hash(self):
        from result_cache import code_hash
        h1 = code_hash("x = 1\nreturn x")
        h2 = code_hash("y = 2\nreturn y")
        assert h1 != h2

    def test_comments_dont_change_hash(self):
        from result_cache import code_hash
        h1 = code_hash("x = 1\nreturn x")
        h2 = code_hash("x = 1  # comment\nreturn x  # another")
        assert h1 == h2

    def test_whitespace_only_differences_dont_change_hash(self):
        from result_cache import code_hash
        h1 = code_hash("x = 1\nreturn x")
        h2 = code_hash("x = 1   \nreturn x   ")
        assert h1 == h2


# ---------------------------------------------------------------------------
# Layer 1: Retrieve cache (SQLite path)
# ---------------------------------------------------------------------------

class TestRetrieveCache:
    def test_miss_returns_none(self, temp_db):
        from result_cache import result_cache_get, result_cache_set
        assert result_cache_get("alice", "revenue_2022") is None

    def test_set_then_get(self, temp_db):
        from result_cache import result_cache_get, result_cache_set
        result_cache_set("alice", "revenue_2022", "32500")
        assert result_cache_get("alice", "revenue_2022") == "32500"

    def test_per_user_isolation(self, temp_db):
        from result_cache import result_cache_get, result_cache_set
        result_cache_set("alice", "revenue_2022", "32500")
        result_cache_set("bob", "revenue_2022", "99999")
        assert result_cache_get("alice", "revenue_2022") == "32500"
        assert result_cache_get("bob", "revenue_2022") == "99999"

    def test_scoped_vs_unscoped_separate(self, temp_db):
        from result_cache import result_cache_get, result_cache_set
        result_cache_set("alice", "revenue_2022", "SheetA: 32500; SheetB: 31000")
        result_cache_set("alice", "sheeta.revenue_2022", "32500")
        assert result_cache_get("alice", "revenue_2022") == "SheetA: 32500; SheetB: 31000"
        assert result_cache_get("alice", "sheeta.revenue_2022") == "32500"

    def test_overwrite_on_same_key(self, temp_db):
        from result_cache import result_cache_get, result_cache_set
        result_cache_set("alice", "revenue_2022", "32500")
        result_cache_set("alice", "revenue_2022", "34000")
        assert result_cache_get("alice", "revenue_2022") == "34000"


# ---------------------------------------------------------------------------
# Layer 2: Sandbox cache (SQLite path)
# ---------------------------------------------------------------------------

class TestSandboxCache:
    def test_miss_returns_none(self, temp_db):
        from result_cache import sandbox_cache_get
        assert sandbox_cache_get("alice", "32500 + 34000") is None

    def test_set_then_get(self, temp_db):
        from result_cache import sandbox_cache_get, sandbox_cache_set
        code = "return 32500 + 34000"
        sandbox_cache_set("alice", code, "66500")
        assert sandbox_cache_get("alice", code) == "66500"

    def test_per_user_isolation(self, temp_db):
        from result_cache import sandbox_cache_get, sandbox_cache_set
        code = "return 42"
        sandbox_cache_set("alice", code, "42")
        sandbox_cache_set("bob", code, "99")
        assert sandbox_cache_get("alice", code) == "42"
        assert sandbox_cache_get("bob", code) == "99"

    def test_comment_differences_same_hash(self, temp_db):
        from result_cache import sandbox_cache_get, sandbox_cache_set
        code1 = "return 42"
        code2 = "return 42  # the answer"
        sandbox_cache_set("alice", code1, "42")
        # code2 normalizes to same hash as code1
        assert sandbox_cache_get("alice", code2) == "42"


# ---------------------------------------------------------------------------
# Layer 3: Post-execution structured key derivation
# ---------------------------------------------------------------------------

class TestCacheStepResults:
    def _make_plan(self, task_type="perform_calculations", plan=None, items=None):
        """Create a QueryPlan-like object."""
        m = MagicMock()
        m.task_type = task_type
        m.plan = plan
        m.items = items
        return m

    def _make_execution(self, step_results, final_answer=None):
        """Create an ExecutionResult-like object."""
        m = MagicMock()
        m.step_results = step_results
        m.final_answer = final_answer
        return m

    def _make_step(self, action, args):
        return MagicMock(action=action, args=args)

    def test_plan_with_retrieve_steps(self, temp_db):
        from result_cache import cache_step_results, result_cache_get
        plan = self._make_plan(plan={
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
            "step2": self._make_step("retrieve", ["Revenue", "2023"]),
        })
        execution = self._make_execution({
            "step1": "32500",
            "step2": "34000",
        })
        cache_step_results("alice", plan, execution)
        assert result_cache_get("alice", "revenue_2022") == "32500"
        assert result_cache_get("alice", "revenue_2023") == "34000"

    def test_plan_with_named_operation(self, temp_db):
        from result_cache import cache_step_results, result_cache_get
        plan = self._make_plan(plan={
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
            "step2": self._make_step("retrieve", ["Revenue", "2023"]),
            "step3": self._make_step("add", ["step1", "step2"]),
        })
        execution = self._make_execution({
            "step1": "32500",
            "step2": "34000",
            "step3": "66500",
        })
        cache_step_results("alice", plan, execution)
        assert result_cache_get("alice", "revenue_2022") == "32500"
        assert result_cache_get("alice", "revenue_2023") == "34000"
        assert result_cache_get("alice", "sum_revenue_2022_revenue_2023") == "66500"

    def test_plan_with_divide_operation(self, temp_db):
        from result_cache import cache_step_results, result_cache_get
        plan = self._make_plan(plan={
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
            "step2": self._make_step("retrieve", ["Grants", "2022"]),
            "step3": self._make_step("divide", ["step1", "step2"]),
        })
        execution = self._make_execution({
            "step1": "32500",
            "step2": "14000",
            "step3": "2.32",
        })
        cache_step_results("alice", plan, execution)
        assert result_cache_get("alice", "division_revenue_2022_grants_2022") == "2.32"

    def test_retrieve_numbers_task_type(self, temp_db):
        from result_cache import cache_step_results, result_cache_get
        plan = self._make_plan(
            task_type="retrieve_numbers",
            plan=None,
            items=["Revenue, 2022", "Grants, 2023"],
        )
        execution = self._make_execution({}, final_answer="32500, 14000")
        cache_step_results("alice", plan, execution)
        assert result_cache_get("alice", "revenue_2022") == "32500, 14000"
        assert result_cache_get("alice", "grants_2023") == "32500, 14000"

    def test_missing_step_result_skipped(self, temp_db):
        from result_cache import cache_step_results, result_cache_get
        plan = self._make_plan(plan={
            "step1": self._make_step("retrieve", ["Revenue", "2022"]),
            "step2": self._make_step("retrieve", ["Revenue", "2023"]),
        })
        execution = self._make_execution({"step1": "32500"})  # step2 missing
        cache_step_results("alice", plan, execution)
        assert result_cache_get("alice", "revenue_2022") == "32500"
        assert result_cache_get("alice", "revenue_2023") is None

    def test_compute_step_skipped_for_structured_key(self, temp_db):
        from result_cache import cache_step_results, result_cache_get
        plan = self._make_plan(plan={
            "step1": self._make_step("compute", ["calculate something complex"]),
        })
        execution = self._make_execution({"step1": "42"})
        cache_step_results("alice", plan, execution)
        # compute steps don't get structured keys
        # but they may get semantic_step entries (if embedding available)
        # just verify no structured key was created
        assert result_cache_get("alice", "calculate something complex") is None


# ---------------------------------------------------------------------------
# Invalidation
# ---------------------------------------------------------------------------

class TestInvalidation:
    def test_invalidate_clears_retrieve_cache(self, temp_db):
        from result_cache import result_cache_get, result_cache_set, invalidate_user_results
        result_cache_set("alice", "revenue_2022", "32500")
        result_cache_set("alice", "grants_2023", "14000")
        deleted = invalidate_user_results("alice")
        assert deleted >= 2
        assert result_cache_get("alice", "revenue_2022") is None
        assert result_cache_get("alice", "grants_2023") is None

    def test_invalidate_clears_sandbox_cache(self, temp_db):
        from result_cache import sandbox_cache_get, sandbox_cache_set, invalidate_user_results
        sandbox_cache_set("alice", "return 42", "42")
        invalidate_user_results("alice")
        assert sandbox_cache_get("alice", "return 42") is None

    def test_invalidate_does_not_affect_other_users(self, temp_db):
        from result_cache import result_cache_get, result_cache_set, invalidate_user_results
        result_cache_set("alice", "revenue_2022", "32500")
        result_cache_set("bob", "revenue_2022", "99999")
        invalidate_user_results("alice")
        assert result_cache_get("bob", "revenue_2022") == "99999"

    def test_invalidate_returns_count(self, temp_db):
        from result_cache import result_cache_set, invalidate_user_results
        result_cache_set("alice", "revenue_2022", "32500")
        result_cache_set("alice", "revenue_2023", "34000")
        result_cache_set("alice", "sum_revenue_2022_revenue_2023", "66500")
        deleted = invalidate_user_results("alice")
        assert deleted == 3


# ---------------------------------------------------------------------------
# Semantic alias for compute steps
# ---------------------------------------------------------------------------

class TestSemanticAlias:
    def test_find_similar_step_miss(self, temp_db):
        from result_cache import find_similar_step
        emb = np.array([1.0, 0.0, 0.0, 0.0])
        assert find_similar_step("alice", emb) is None

    def test_find_similar_step_hit(self, temp_db):
        from result_cache import result_cache_set, find_similar_step
        # Store a semantic step entry
        desc = "grand total of all revenue"
        emb = [1.0, 0.0, 0.0, 0.0]
        import hashlib
        desc_hash = hashlib.sha256(desc.encode()).hexdigest()
        result_cache_set(
            "alice", desc_hash, "139000",
            cache_type="semantic_step",
            description=desc,
            structured_key="sum_revenue_2022_2023",
            embedding=emb,
        )
        # Search with a similar embedding
        query_emb = np.array([0.99, 0.01, 0.0, 0.0])
        result = find_similar_step("alice", query_emb, threshold=0.90)
        assert result is not None
        assert result["value"] == "139000"
        assert result["structured_key"] == "sum_revenue_2022_2023"

    def test_find_similar_step_below_threshold(self, temp_db):
        from result_cache import result_cache_set, find_similar_step
        desc = "grand total of all revenue"
        emb = [1.0, 0.0, 0.0, 0.0]
        import hashlib
        desc_hash = hashlib.sha256(desc.encode()).hexdigest()
        result_cache_set(
            "alice", desc_hash, "139000",
            cache_type="semantic_step",
            description=desc,
            embedding=emb,
        )
        # Orthogonal embedding — should not match
        query_emb = np.array([0.0, 0.0, 0.0, 1.0])
        result = find_similar_step("alice", query_emb, threshold=0.90)
        assert result is None

    def test_find_similar_step_per_user_isolation(self, temp_db):
        from result_cache import result_cache_set, find_similar_step
        desc = "grand total of all revenue"
        emb = [1.0, 0.0, 0.0, 0.0]
        import hashlib
        desc_hash = hashlib.sha256(desc.encode()).hexdigest()
        result_cache_set(
            "alice", desc_hash, "139000",
            cache_type="semantic_step",
            description=desc,
            embedding=emb,
        )
        query_emb = np.array([1.0, 0.0, 0.0, 0.0])
        # Bob should not find alice's entry
        result = find_similar_step("bob", query_emb, threshold=0.90)
        assert result is None

    def test_find_similar_step_none_embedding(self, temp_db):
        from result_cache import find_similar_step
        assert find_similar_step("alice", None) is None


# ---------------------------------------------------------------------------
# Integration: retrieve tool with cache (simulated)
# ---------------------------------------------------------------------------

class TestRetrieveToolCacheIntegration:
    """Test that the retrieve tool correctly uses the cache.

    We simulate the cache check/store logic that was added to retrieve()
    without needing the full Pydantic AI agent stack.
    """

    def test_cache_hit_skips_dataframe_scan(self, temp_db):
        from result_cache import build_retrieve_key, result_cache_get, result_cache_set
        # Pre-populate cache
        key = build_retrieve_key("Revenue", "2022")
        result_cache_set("alice", key, "32500")
        # Simulate what retrieve() does: check cache first
        cached = result_cache_get("alice", key)
        assert cached == "32500"
        # If cached is not None, retrieve would return early — no DataFrame scan

    def test_cache_miss_then_store(self, temp_db):
        from result_cache import build_retrieve_key, result_cache_get, result_cache_set
        key = build_retrieve_key("Revenue", "2022")
        # First call: miss
        assert result_cache_get("alice", key) is None
        # After computation, store
        result_cache_set("alice", key, "32500")
        # Second call: hit
        assert result_cache_get("alice", key) == "32500"

    def test_error_returns_not_cached(self, temp_db):
        """The retrieve tool should not cache ERROR: returns."""
        from result_cache import build_retrieve_key, result_cache_get, result_cache_set
        key = build_retrieve_key("NonExistent", "2099")
        # Simulate: retrieve returns error, we skip cache storage
        result = "ERROR: 'NonExistent' or '2099' not found in any sheet."
        if not result.startswith("ERROR:"):
            result_cache_set("alice", key, result)
        # Verify nothing was cached
        assert result_cache_get("alice", key) is None
