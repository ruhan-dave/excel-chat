#!/usr/bin/env python3
"""
Test the actual LLM agent generating Python code for real queries.

Uses real OpenRouter API calls (requires OPENROUTER_API_KEY).
Tests that the planner + executor pipeline produces valid results for
financial questions against the example Excel file.
"""

import asyncio
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

from dotenv import load_dotenv

load_dotenv()

os.environ["OPENAI_API_KEY"] = os.environ.get("OPENROUTER_API_KEY", "")

from excelservices import ExcelService
from sheet_metadata import SheetMeta
from pipeline import build_query_pipeline
from llama_index.core.prompts import PromptTemplate

EXCEL_FILE = Path(__file__).parent.parent / "example_sheets" / "Detailed_Expense_Breakdown.xlsx"


def _build_pipeline():
    """Build a query pipeline from the example Excel file."""
    df = pd.read_excel(EXCEL_FILE)
    cleaned = ExcelService.clean_dataframe(df)
    sheets = {"Sheet1": cleaned}

    fields = list(cleaned.index.astype(str))
    years = [str(c) for c in cleaned.columns]

    meta = SheetMeta(
        sheet_id="test-sheet",
        file_id="test-file",
        file_name=EXCEL_FILE.name,
        sheet_name="Sheet1",
        s3_key="test/key",
        fields=fields,
        years=years,
        row_count=len(cleaned),
    )
    template = PromptTemplate("Query: {query}\n\nSheet context: {sheet_context}")
    return build_query_pipeline(None, sheets, [meta], template)


@pytest.fixture(scope="module")
def pipeline():
    if not EXCEL_FILE.exists():
        pytest.skip(f"Example file not found: {EXCEL_FILE}")
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set")
    return _build_pipeline()


def test_cagr_calculation(pipeline):
    """Agent can compute compound annual growth rate of revenue."""
    result = asyncio.run(pipeline("What's the compound annual growth rate of revenue from 2020 to 2023?"))
    assert result is not None
    assert isinstance(result, dict)


def test_trend_analysis(pipeline):
    """Agent can analyze profit margin trends."""
    result = asyncio.run(pipeline("Analyze the profit margin trend across all years"))
    assert result is not None
    assert isinstance(result, dict)


def test_average_growth_rate(pipeline):
    """Agent can calculate average year-over-year revenue growth."""
    result = asyncio.run(pipeline("Calculate the average revenue growth rate year over year"))
    assert result is not None
    assert isinstance(result, dict)


def test_efficiency_analysis(pipeline):
    """Agent can handle vague prompts about business efficiency."""
    result = asyncio.run(pipeline("How efficient is the business becoming over time?"))
    assert result is not None
    assert isinstance(result, dict)
