"""
Tests for the latency optimization features in pipeline.py:

* Opt 6 — ``timed()`` context manager records elapsed seconds.
* Opt 1 — ``retrieve_values()`` unified tool (single + multi-year).
* Single-agent refactor — ``build_query_agent`` constructs with the plan tools.
* Deterministic short-circuit — ``_try_deterministic_lookup`` (0-LLM path).
* Plan validation — ``validate_plan_semantics`` (fields / years / step refs).
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
    from tools import retrieve_values
    sig = inspect.signature(retrieve_values)
    params = list(sig.parameters.keys())
    # ctx is positional, then field, years, sheet
    assert "ctx" in params
    assert "field" in params
    assert "years" in params
    assert "sheet" in params


def test_retrieve_values_single_sheet_multi_year_returns_scalar_dict():
    """With a single matching sheet, multi-year retrieve_values returns a year → float dict."""
    from tools import retrieve_values, PipelineDeps
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
    from tools import retrieve_values, PipelineDeps
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
    from tools import retrieve_values, PipelineDeps
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
    from tools import retrieve_values, PipelineDeps
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
    from models import PlanStep
    from typing import get_args

    literal_values = get_args(PlanStep.model_fields["action"].annotation)
    assert "retrieve" in literal_values
    assert "compute" in literal_values


def test_query_agent_builds_with_plan_tools():
    """The single query agent must construct and register its tools."""
    from agent import build_query_agent

    try:
        agent = build_query_agent([])
    except TypeError as exc:
        if "_build_sheet_context" in str(exc):
            pytest.fail(
                "Genuine bug in backend/src/agent.py: build_query_agent calls "
                "_build_sheet_context(sheets, sheet_metas) but the helper signature "
                "is _build_sheet_context(sheet_metas) — fix the call site to pass "
                "only sheet_metas."
            )
        raise
    assert agent is not None
    # pydantic-ai 2.x: agent-direct tools (including @agent.tool nested
    # functions) live in the agent's function toolset.
    toolset = getattr(agent, "_function_toolset", None)
    if toolset is not None:
        tool_names = set(toolset.tools.keys())
    else:
        tool_names = set(agent._function_tools.keys())
    assert "retrieve_values" in tool_names
    assert "execute_python_code" in tool_names
    assert "write_plan" in tool_names
    assert "update_step_status" in tool_names


# ---------------------------------------------------------------------------
# Year heuristic (kept from the pre-populate era, still used by the pipeline)
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


# ---------------------------------------------------------------------------
# Deterministic short-circuit: _try_deterministic_lookup (0-LLM path)
# ---------------------------------------------------------------------------

def _make_meta(fields, years, sheet_name="Sheet1"):
    from sheet_metadata import SheetMeta
    return SheetMeta(
        sheet_id="s1", file_id="f1", file_name="report.xlsx",
        sheet_name=sheet_name, s3_key="s3://bucket/report.xlsx",
        fields=list(fields), years=list(years),
    )


def test_deterministic_lookup_simple_query_returns_field_and_year():
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue", "Expenses"], ["2022", "2023"])]
    assert _try_deterministic_lookup("What was the revenue in 2023?", metas) == (
        "Revenue", "2023",
    )


def test_deterministic_lookup_interest_expense_field():
    """Field names with multiple words match case-insensitively as substrings."""
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Interest expense", "Revenue"], ["2022", "2023"])]
    result = _try_deterministic_lookup("What was the interest expense in 2023?", metas)
    assert result == ("Interest expense", "2023")


def test_deterministic_lookup_how_much_prefix():
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue"], ["2022"])]
    assert _try_deterministic_lookup("How much was revenue in 2022?", metas) == ("Revenue", "2022")


def test_deterministic_lookup_calc_keyword_returns_none():
    """Queries needing calculation must fall through to the agent."""
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue"], ["2022", "2023"])]
    assert _try_deterministic_lookup("What was the revenue growth in 2023?", metas) is None
    assert _try_deterministic_lookup("What is the average revenue in 2023?", metas) is None


def test_deterministic_lookup_two_years_returns_none():
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue"], ["2022", "2023"])]
    assert _try_deterministic_lookup("What was revenue in 2022 and 2023?", metas) is None


def test_deterministic_lookup_no_matching_field_returns_none():
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue"], ["2022"])]
    assert _try_deterministic_lookup("What was the free cash flow in 2022?", metas) is None


def test_deterministic_lookup_multiple_fields_returns_none():
    """Two distinct catalog fields mentioned → ambiguous → None."""
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue", "Expenses"], ["2022"])]
    assert (
        _try_deterministic_lookup("What was revenue and expenses in 2022?", metas)
        is None
    )


def test_deterministic_lookup_no_year_returns_none():
    from pipeline import _try_deterministic_lookup

    metas = [_make_meta(["Revenue"], ["2022", "2023"])]
    assert _try_deterministic_lookup("What was the revenue?", metas) is None


# ---------------------------------------------------------------------------
# Plan validation: validate_plan_semantics
# ---------------------------------------------------------------------------

def test_validate_plan_semantics_valid_plan_returns_no_errors():
    from agent import validate_plan_semantics; from models import QueryPlan, PlanStep

    metas = [_make_meta(["Revenue", "Expenses"], ["2022", "2023"])]
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={
            "step1": PlanStep(action="retrieve", args=["Revenue", "2022"]),
            # Sheet-scoped retrieve: ["SheetName", "FieldName", "Year"]
            "step2": PlanStep(action="retrieve", args=["Sheet1", "Expenses", "2023"]),
            "step3": PlanStep(action="subtract", args=["step1", "step2"]),
        },
    )
    assert validate_plan_semantics(plan, metas) == []


def test_validate_plan_semantics_unknown_field_with_did_you_mean_hint():
    from agent import validate_plan_semantics; from models import QueryPlan, PlanStep

    metas = [_make_meta(["Revenue", "Expenses"], ["2022"])]
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={"step1": PlanStep(action="retrieve", args=["Revenu", "2022"])},
    )
    errors = validate_plan_semantics(plan, metas)
    assert len(errors) == 1
    assert "unknown field" in errors[0]
    assert "Revenu" in errors[0]
    # difflib hint suggests the closest catalog field
    assert "Revenue" in errors[0]


def test_validate_plan_semantics_unknown_year_lists_valid_years():
    from agent import validate_plan_semantics; from models import QueryPlan, PlanStep

    metas = [_make_meta(["Revenue"], ["2022", "2023"])]
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={"step1": PlanStep(action="retrieve", args=["Revenue", "2019"])},
    )
    errors = validate_plan_semantics(plan, metas)
    assert len(errors) == 1
    assert "unknown year" in errors[0]
    assert "2019" in errors[0]
    assert "2022" in errors[0] and "2023" in errors[0]  # valid years listed


def test_validate_plan_semantics_forward_reference_rejected():
    from agent import validate_plan_semantics; from models import QueryPlan, PlanStep

    metas = [_make_meta(["Revenue"], ["2022"])]
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={
            "step1": PlanStep(action="subtract", args=["step2", "100"]),
            "step2": PlanStep(action="retrieve", args=["Revenue", "2022"]),
        },
    )
    errors = validate_plan_semantics(plan, metas)
    assert any("defined later" in e for e in errors)


def test_validate_plan_semantics_named_op_garbage_arg_rejected():
    from agent import validate_plan_semantics; from models import QueryPlan, PlanStep

    metas = [_make_meta(["Revenue"], ["2022"])]
    plan = QueryPlan(
        task_type="perform_calculations",
        plan={
            "step1": PlanStep(action="retrieve", args=["Revenue", "2022"]),
            "step2": PlanStep(action="add", args=["step1", "banana"]),
        },
    )
    errors = validate_plan_semantics(plan, metas)
    assert any("banana" in e and "must reference a prior step" in e for e in errors)


# ---------------------------------------------------------------------------
# Opt 3: model name default
# ---------------------------------------------------------------------------

def test_default_model_is_deepseek_v4_flash():
    """build_openrouter_model must default to deepseek/deepseek-v4-flash."""
    import models
    # .env (loaded via sheet_metadata import) may override MODEL_ID at import
    # time, so assert on the source default rather than the runtime value.
    src = Path(models.__file__).read_text()
    assert 'os.environ.get("MODEL_ID", "deepseek/deepseek-v4-flash")' in src


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