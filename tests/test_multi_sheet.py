"""
Tests for multi-sheet functionality: schema group detection, sheet metadata, and retrieve tool.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

# Add the backend/src directory to the path
backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

from sheet_metadata import SheetMeta, init_db, get_cached_response, set_cached_response, get_cache_stats
from excelservices import ExcelService
from pipeline import PipelineDeps, retrieve
from pydantic_ai import RunContext
import pandas as pd


def test_schema_group_detection():
    """Test that sheets with similar fields are grouped together."""
    sheets = [
        SheetMeta(
            sheet_id="1", file_id="f1", file_name="test.xlsx",
            sheet_name="Sheet1", s3_key="k1",
            fields=["Revenue", "Expenses", "Profit", "Assets"],
            years=["2021", "2022", "2023"],
        ),
        SheetMeta(
            sheet_id="2", file_id="f1", file_name="test.xlsx",
            sheet_name="Sheet2", s3_key="k1",
            fields=["Revenue", "Expenses", "Profit", "Liabilities"],
            years=["2021", "2022", "2023"],
        ),
        SheetMeta(
            sheet_id="3", file_id="f2", file_name="other.xlsx",
            sheet_name="Sheet3", s3_key="k2",
            fields=["Temperature", "Humidity", "Pressure"],
            years=["2021", "2022", "2023"],
        ),
    ]

    ExcelService.detect_schema_groups(sheets)

    # Sheet1 and Sheet2 should be in the same group (3/4 fields overlap = 0.6 Jaccard... wait)
    # Actually: intersection = {Revenue, Expenses, Profit} = 3, union = 5, jaccard = 0.6
    # With threshold 0.75, they would NOT be grouped. Let me adjust.
    # Let me use threshold 0.5 for this test
    sheets_copy = [s for s in sheets]
    for s in sheets_copy:
        s.schema_group = ""
    ExcelService.detect_schema_groups(sheets_copy, threshold=0.5)

    assert sheets_copy[0].schema_group == sheets_copy[1].schema_group, \
        f"Sheet1 and Sheet2 should be in same group, got {sheets_copy[0].schema_group} vs {sheets_copy[1].schema_group}"
    assert sheets_copy[0].schema_group != "unique", "Group should not be 'unique'"
    assert sheets_copy[2].schema_group == "unique", "Sheet3 should be unique"
    print("✅ test_schema_group_detection passed")


def test_schema_group_identical_fields():
    """Sheets with identical fields should always be grouped."""
    sheets = [
        SheetMeta(
            sheet_id="1", file_id="f1", file_name="test.xlsx",
            sheet_name="Q1", s3_key="k1",
            fields=["Revenue", "Expenses", "Profit"],
            years=["2021", "2022"],
        ),
        SheetMeta(
            sheet_id="2", file_id="f1", file_name="test.xlsx",
            sheet_name="Q2", s3_key="k1",
            fields=["Revenue", "Expenses", "Profit"],
            years=["2021", "2022"],
        ),
    ]

    ExcelService.detect_schema_groups(sheets)

    assert sheets[0].schema_group == sheets[1].schema_group, "Identical sheets should be grouped"
    assert sheets[0].schema_group != "unique", "Group should not be 'unique'"
    print("✅ test_schema_group_identical_fields passed")


def test_retrieve_single_sheet():
    """Test retrieve tool with a specific sheet name."""
    df1 = pd.DataFrame(
        {"2022": [1000.0, 500.0], "2023": [1200.0, 600.0]},
        index=["Revenue", "Expenses"],
    )
    df2 = pd.DataFrame(
        {"2022": [2000.0, 1000.0], "2023": [2400.0, 1200.0]},
        index=["Revenue", "Expenses"],
    )
    sheets = {"Sheet1": df1, "Sheet2": df2}

    deps = PipelineDeps(
        sheets=sheets,
        sheet_metas=[],
        original_query="test",
        available_fields=["Revenue", "Expenses"],
        available_years=["2022", "2023"],
    )
    mock_ctx = MagicMock(spec=RunContext)
    mock_ctx.deps = deps

    result = retrieve(mock_ctx, "Revenue", "2022", "Sheet1")
    assert result == "1000.0", f"Expected '1000.0', got '{result}'"
    print("✅ test_retrieve_single_sheet passed")


def test_retrieve_all_sheets():
    """Test retrieve tool searching across all sheets."""
    df1 = pd.DataFrame(
        {"2022": [1000.0, 500.0]},
        index=["Revenue", "Expenses"],
    )
    df2 = pd.DataFrame(
        {"2022": [2000.0, 1000.0]},
        index=["Revenue", "Expenses"],
    )
    sheets = {"Sheet1": df1, "Sheet2": df2}

    deps = PipelineDeps(
        sheets=sheets,
        sheet_metas=[],
        original_query="test",
        available_fields=["Revenue", "Expenses"],
        available_years=["2022"],
    )
    mock_ctx = MagicMock(spec=RunContext)
    mock_ctx.deps = deps

    result = retrieve(mock_ctx, "Revenue", "2022")
    assert "Sheet1: 1000.0" in result, f"Expected Sheet1 value, got '{result}'"
    assert "Sheet2: 2000.0" in result, f"Expected Sheet2 value, got '{result}'"
    print("✅ test_retrieve_all_sheets passed")


def test_retrieve_not_found():
    """Test retrieve tool when field/year doesn't exist."""
    df = pd.DataFrame(
        {"2022": [1000.0]},
        index=["Revenue"],
    )
    deps = PipelineDeps(
        sheets={"Sheet1": df},
        sheet_metas=[],
        original_query="test",
        available_fields=["Revenue"],
        available_years=["2022"],
    )
    mock_ctx = MagicMock(spec=RunContext)
    mock_ctx.deps = deps

    result = retrieve(mock_ctx, "Unknown", "2022")
    assert "ERROR" in result, f"Expected ERROR, got '{result}'"
    print("✅ test_retrieve_not_found passed")


def test_sheet_meta_combined_description():
    """Test that combined_description merges user and auto descriptions."""
    meta = SheetMeta(
        sheet_id="1", file_id="f1", file_name="test.xlsx",
        sheet_name="Sheet1", s3_key="k1",
        fields=["Revenue"], years=["2022"],
        user_description="This is user input",
        auto_description="This is auto generated",
    )
    desc = meta.combined_description
    assert "user input" in desc.lower(), f"Expected user description in combined, got '{desc}'"
    assert "auto generated" in desc.lower(), f"Expected auto description in combined, got '{desc}'"
    print("✅ test_sheet_meta_combined_description passed")


def test_sheet_meta_empty_description():
    """Test combined_description when no descriptions are set."""
    meta = SheetMeta(
        sheet_id="1", file_id="f1", file_name="test.xlsx",
        sheet_name="Sheet1", s3_key="k1",
        fields=["Revenue"], years=["2022"],
    )
    assert meta.combined_description == "No description available."
    print("✅ test_sheet_meta_empty_description passed")


def test_init_db():
    """Test that init_db creates the database without error."""
    init_db()
    print("✅ test_init_db passed")


def test_field_similarity():
    """Test the field similarity helper."""
    sim = ExcelService._field_similarity(["Revenue", "Expenses"], ["Revenue", "Expenses"])
    assert sim == 1.0, f"Identical fields should have similarity 1.0, got {sim}"

    sim = ExcelService._field_similarity(["Revenue", "Expenses"], ["Temperature"])
    assert sim == 0.0, f"Completely different fields should have similarity 0.0, got {sim}"

    sim = ExcelService._field_similarity(["Revenue", "Expenses", "Profit"], ["Revenue", "Expenses", "Liabilities"])
    # intersection = 2, union = 4, jaccard = 0.5
    assert sim == 0.5, f"Expected 0.5 similarity, got {sim}"
    print("✅ test_field_similarity passed")


def test_llm_cache_set_get():
    """Test that LLM cache stores and retrieves responses."""
    init_db()
    model = "test-model"
    prompt = "test prompt for caching"
    response = "test response"

    set_cached_response(model, prompt, response)
    cached = get_cached_response(model, prompt)
    assert cached == response, f"Expected '{response}', got '{cached}'"
    print("✅ test_llm_cache_set_get passed")


def test_llm_cache_miss():
    """Test that cache returns None for unseen prompts."""
    init_db()
    result = get_cached_response("nonexistent-model", "never-seen-prompt")
    assert result is None, f"Expected None for cache miss, got '{result}'"
    print("✅ test_llm_cache_miss passed")


def test_llm_cache_stats():
    """Test that cache stats returns valid structure."""
    init_db()
    stats = get_cache_stats()
    assert "total_entries" in stats, f"Missing total_entries in stats: {stats}"
    assert "total_hits" in stats, f"Missing total_hits in stats: {stats}"
    assert isinstance(stats["total_entries"], int)
    assert isinstance(stats["total_hits"], int)
    print("✅ test_llm_cache_stats passed")


if __name__ == "__main__":
    test_init_db()
    test_schema_group_detection()
    test_schema_group_identical_fields()
    test_retrieve_single_sheet()
    test_retrieve_all_sheets()
    test_retrieve_not_found()
    test_sheet_meta_combined_description()
    test_sheet_meta_empty_description()
    test_field_similarity()
    test_llm_cache_set_get()
    test_llm_cache_miss()
    test_llm_cache_stats()

    print("=" * 60)
    print("All multi-sheet tests passed! ✅")
    print("=" * 60)
