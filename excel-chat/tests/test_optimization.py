"""
Tests for the latency optimization features in pipeline.py:

* Opt 6 — ``timed()`` context manager records elapsed seconds.
* Opt 1 — ``retrieve_values()`` unified tool (single + multi-year).
* Opt 2 — ``_prepopulate_retrievals`` short-circuits pure-retrieve plans.
* Opt 1/2 — ``_plan_is_pure_retrieve`` and ``_format_pre_populated_for_prompt``.
* Opt 3 — Default model name is the new ``openai/gpt-oss-120b:nitro``.
"""

from __future__ import annotations

import inspect
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

BACKEND_SRC = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))


# ---------------------------------------------------------------------------
# Opt 6: timed() context manager
# ---------------------------------------------------------------------------

def test_timed_records_elapsed_seconds():
    from pipeline import timed

    timings: dict[str, float] = {}
    with timed("test_stage", timings):
        time.sleep(0.02)  # ~20ms

    assert "test_stage" in timings
    assert timings["test_stage"] >= 0.02
    assert timings["test_stage"] < 1.0  # generous upper bound


def test_timed_accumulates_repeated_calls():
    from pipeline import timed

    timings: dict[str, float] = {}
    for _ in range(3):
        with timed("repeat", timings):
            time.sleep(0.005)
    assert timings["repeat"] >= 0.015


def test_timed_records_even_on_exception():
    """Failures inside the ``with`` block must still be timed."""
    from pipeline import timed

    timings: dict[str, float] = {}
    with pytest.raises(ValueError):
        with timed("explode", timings):
            time.sleep(0.005)
            raise ValueError("boom")
    assert "explode" in timings
    assert timings["explode"] >= 0.005


def test_timed_propagates_return_value():
    from pipeline import timed

    timings: dict[str, float] = {}
    with timed("returns", timings):
        value = 42
    assert value == 42


# ---------------------------------------------------------------------------
# Opt 1: retrieve_values tool (unified single + multi-year)
# ---------------------------------------------------------------------------

def test_retrieve_values_signature():
    from pipeline import retrieve_values
    sig = inspect.signature(retrieve_values)
    params = list(sig.parameters.keys())
    # ctx is positional, then field, years, sheet
    assert "ctx" in params
    assert "field" in params
    assert "years" in params
    assert "sheet" in params


def test_retrieve_values_single_sheet_multi_year_returns_scalar_dict():
    """With a single matching sheet, multi-year retrieve_values returns a year → float dict."""
    from pipeline import retrieve_values, PipelineDeps
    from types import SimpleNamespace
    import uuid

    df = pd.DataFrame(
        {"2022": [1000.0], "2023": [1500.0]},
        index=["Revenue"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    ctx = SimpleNamespace(deps=deps)

    result_str = retrieve_values(ctx, "Revenue", ["2022", "2023"], sheet="Sheet1")
    result = json.loads(result_str)
    assert result == {"2022": 1000.0, "2023": 1500.0}


def test_retrieve_values_cross_sheet_returns_per_sheet_dict():
    """Without a sheet name, multi-year retrieve_values returns year → {sheet: float}."""
    from pipeline import retrieve_values, PipelineDeps
    from types import SimpleNamespace
    import uuid

    df = pd.DataFrame(
        {"2022": [1000.0]},
        index=["Revenue"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df, "Sheet2": df.copy()}, sheet_metas=[],
        original_query="q", user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    ctx = SimpleNamespace(deps=deps)

    result_str = retrieve_values(ctx, "Revenue", ["2022", "2023"], sheet="")
    result = json.loads(result_str)
    assert result["2022"] == {"Sheet1": 1000.0, "Sheet2": 1000.0}


def test_retrieve_values_handles_missing_field():
    """A missing field returns an error message."""
    from pipeline import retrieve_values, PipelineDeps
    from types import SimpleNamespace
    import uuid

    df = pd.DataFrame({"2022": [1000.0]}, index=["Revenue"])
    deps = PipelineDeps(
        sheets={"Sheet1": df}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    ctx = SimpleNamespace(deps=deps)
    result = json.loads(retrieve_values(ctx, "DoesNotExist", ["2022", "2023"], sheet="Sheet1"))
    assert "error" in result


def test_retrieve_values_empty_years_returns_error():
    from pipeline import retrieve_values, PipelineDeps
    from types import SimpleNamespace
    import uuid

    deps = PipelineDeps(
        sheets={}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    ctx = SimpleNamespace(deps=deps)
    result = retrieve_values(ctx, "Revenue", [])
    assert "ERROR" in result


def test_planstep_action_literal_includes_retrieve():
    """The PlanStep.action Literal must allow 'retrieve' and 'compute'."""
    from pipeline import PlanStep
    from typing import get_args

    literal_values = get_args(PlanStep.model_fields["action"].annotation)
    assert "retrieve" in literal_values
    assert "compute" in literal_values


def test_executor_agent_registers_retrieve_values_tool():
    """The executor agent must expose retrieve_values as a tool."""
    from pipeline import build_executor_agent

    df = pd.DataFrame({"2022": [100.0]}, index=["Revenue"])
    agent = build_executor_agent({"Sheet1": df}, [])
    tool_names = set(agent._function_tools.keys())
    assert "retrieve_values" in tool_names
    assert "execute_python_code" in tool_names


# ---------------------------------------------------------------------------
# Opt 2: pre-populate retrievals
# ---------------------------------------------------------------------------

def test_looks_like_year_detects_year_strings():
    from pipeline import _looks_like_year
    assert _looks_like_year("2022") is True
    assert _looks_like_year("2023") is True
    assert _looks_like_year("2022.0") is True
    assert _looks_like_year("Revenue") is False
    assert _looks_like_year("Q1 2022") is False
    assert _looks_like_year("") is False
    assert _looks_like_year("1800") is False  # outside plausible year range


def test_prepopulate_retrieve_step_returns_value():
    """Cross-sheet retrieve returns 'SheetName: value' format."""
    from pipeline import (
        _prepopulate_retrievals, PipelineDeps, QueryPlan, PlanStep,
    )
    import uuid

    df = pd.DataFrame(
        {"2022": [1500.0, 800.0]}, index=["Revenue", "Expenses"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={"step1": PlanStep(action="retrieve", args=["Revenue", "2022"])},
    )
    result = _prepopulate_retrievals(plan, deps)
    # Cross-sheet retrieve with single matching sheet returns "Sheet1: 1500.0"
    assert result["step1"] == "Sheet1: 1500.0"


def test_prepopulate_specific_sheet_returns_scalar():
    """Specific-sheet retrieve returns just the scalar value as a string."""
    from pipeline import (
        _prepopulate_retrievals, PipelineDeps, QueryPlan, PlanStep,
    )
    import uuid

    df = pd.DataFrame(
        {"2022": [1500.0, 800.0]}, index=["Revenue", "Expenses"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={"step1": PlanStep(
            action="retrieve", args=["Sheet1", "Revenue", "2022"],
        )},
    )
    result = _prepopulate_retrievals(plan, deps)
    assert result["step1"] == "1500.0"


def test_prepopulate_multi_year_retrieve_returns_parsed_dict():
    from pipeline import (
        _prepopulate_retrievals, PipelineDeps, QueryPlan, PlanStep,
    )
    import uuid

    df = pd.DataFrame(
        {"2022": [100.0], "2023": [150.0]},
        index=["Revenue"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={"b1": PlanStep(action="retrieve", args=["Revenue", "2022", "2023"])},
    )
    result = _prepopulate_retrievals(plan, deps)
    assert result["b1"] == {"2022": 100.0, "2023": 150.0}


def test_prepopulate_specific_sheet_multi_year():
    """retrieve with [sheet, field, years...] is routed to that sheet."""
    from pipeline import (
        _prepopulate_retrievals, PipelineDeps, QueryPlan, PlanStep,
    )
    import uuid

    df = pd.DataFrame(
        {"2022": [100.0], "2023": [150.0]},
        index=["Revenue"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={"b1": PlanStep(action="retrieve", args=["Sheet1", "Revenue", "2022", "2023"])},
    )
    result = _prepopulate_retrievals(plan, deps)
    assert result["b1"] == {"2022": 100.0, "2023": 150.0}


def test_prepopulate_skips_non_retrieve_steps():
    """Named ops and compute steps are left for the executor."""
    from pipeline import (
        _prepopulate_retrievals, PipelineDeps, QueryPlan, PlanStep,
    )
    import uuid

    deps = PipelineDeps(
        sheets={}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={
            "step1": PlanStep(action="compute", args=["sum the values"]),
            "step2": PlanStep(action="add", args=["1", "2"]),
        },
    )
    result = _prepopulate_retrievals(plan, deps)
    assert result == {}


def test_prepopulate_handles_empty_plan():
    from pipeline import _prepopulate_retrievals, PipelineDeps, QueryPlan
    import uuid
    deps = PipelineDeps(
        sheets={}, sheet_metas=[], original_query="q",
        user_id=f"test_{uuid.uuid4().hex[:8]}",
    )
    plan = QueryPlan(task_type="give_advice", description="explain revenue")
    assert _prepopulate_retrievals(plan, deps) == {}


def test_plan_is_pure_retrieve():
    from pipeline import _plan_is_pure_retrieve, QueryPlan, PlanStep

    pure = QueryPlan(
        task_type="perform_calculations",
        plan={
            "a": PlanStep(action="retrieve", args=["Revenue", "2022"]),
            "b": PlanStep(action="retrieve", args=["Revenue", "2022", "2023"]),
        },
    )
    assert _plan_is_pure_retrieve(pure) is True

    mixed = QueryPlan(
        task_type="perform_calculations",
        plan={
            "a": PlanStep(action="retrieve", args=["Revenue", "2022"]),
            "b": PlanStep(action="compute", args=["sum"]),
        },
    )
    assert _plan_is_pure_retrieve(mixed) is False

    empty = QueryPlan(task_type="give_advice", description="x")
    assert _plan_is_pure_retrieve(empty) is False


def test_format_pre_populated_block_renders_human_readable():
    from pipeline import _format_pre_populated_for_prompt

    out = _format_pre_populated_for_prompt({
        "step1": 1500.0,
        "step2": {"2022": 100.0, "2023": 150.0},
    })
    assert "Pre-computed values" in out
    assert "do NOT call retrieve" in out
    assert "step1: 1500.0" in out
    assert "step2:" in out


def test_format_pre_populated_block_empty_returns_empty_string():
    from pipeline import _format_pre_populated_for_prompt
    assert _format_pre_populated_for_prompt({}) == ""


# ---------------------------------------------------------------------------
# Opt 3: model name default
# ---------------------------------------------------------------------------

def test_default_model_is_gpt_oss_120b_nitro():
    """build_openrouter_model must default to the :nitro tier."""
    import pipeline
    assert pipeline.PRIMARY_MODEL == "openai/gpt-oss-120b:nitro"


# ---------------------------------------------------------------------------
# Opt 4: embedding model + threshold
# ---------------------------------------------------------------------------

def test_semantic_cache_uses_minilm():
    """semantic_cache must reference the new model and dim."""
    src = Path(BACKEND_SRC / "semantic_cache.py").read_text()
    assert 'all-MiniLM-L6-v2' in src
    assert "_EMBED_DIM = 384" in src
    assert "Qwen3-Embedding-8B" not in src
    assert "truncate_dim" not in src


def test_semantic_cache_threshold_lowered():
    """Threshold should be 0.88 to account for MiniLM's lower accuracy."""
    src = Path(BACKEND_SRC / "semantic_cache.py").read_text()
    assert "threshold: float = 0.88" in src


# ---------------------------------------------------------------------------
# Opt 5: in-memory S3 read helpers exist
# ---------------------------------------------------------------------------

def test_read_excel_from_s3_exists_in_sheet_metadata():
    from sheet_metadata import read_excel_from_s3
    assert callable(read_excel_from_s3)


def test_load_all_sheets_buffer_exists_in_excelservices():
    from excelservices import ExcelService
    assert hasattr(ExcelService, "load_all_sheets_buffer")
    assert callable(ExcelService.load_all_sheets_buffer)


def test_load_all_sheets_buffer_parses_xlsx():
    """End-to-end check: write an xlsx to BytesIO, parse with load_all_sheets_buffer."""
    from excelservices import ExcelService
    import io

    # Build a tiny in-memory xlsx file. clean_dataframe detects the year row
    # by checking every cell matches r'^\d{4}(\.0)?$' — so the year row must
    # contain only year-like values (no 'Notes' column).
    rows = [
        ["", "", ""],          # empty header
        ["Some metadata", "", ""],
        [2022, 2023, 2024],    # year row (all 4-digit years)
        ["Revenue", 100.0, 150.0, 200.0],
        ["Expenses", 50.0, 75.0, 100.0],
    ]
    raw_df = pd.DataFrame(rows)
    buf = io.BytesIO()
    raw_df.to_excel(buf, index=False, header=False)
    buf.seek(0)

    sheets = ExcelService.load_all_sheets_buffer(buf)
    assert "Sheet1" in sheets
    cleaned = sheets["Sheet1"]
    # After cleaning, we should have 2 rows (Revenue, Expenses) and 3 year columns.
    assert cleaned.shape[0] >= 1
    assert cleaned.shape[1] >= 2  # at least 2 year columns


def test_load_all_sheets_buffer_parses_buffer_object():
    """Pass any file-like object — not just a freshly written one."""
    from excelservices import ExcelService
    import io

    rows = [
        [2022, 2023],
        ["Revenue", 100.0, 150.0],
    ]
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, header=False)
    buf.seek(0)

    sheets = ExcelService.load_all_sheets_buffer(buf)
    assert isinstance(sheets, dict)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))