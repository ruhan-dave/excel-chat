"""
Tests for the 10 financial questions that ragsheets got wrong last year.

Each test verifies that the pipeline tools (retrieve + execute_python_code)
can answer the question without null values, ERROR returns, or incomplete
steps. The tests use the cleaned DataFrame directly (no LLM call) to verify
the data layer is correct — the LLM agent layer is tested separately.

Data: example_sheets/Detailed_Expense_Breakdown.xlsx (Albania budgetary
central government expenses, 2011–2023, millions of domestic currency).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

# Add backend/src to sys.path
BACKEND_SRC = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))

from pipeline import PipelineDeps, retrieve, execute_python_code
from pydantic_ai import RunContext


def _run(ctx, code):
    """Helper to run async execute_python_code in sync tests."""
    return asyncio.run(execute_python_code(ctx, code))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cleaned_sheets():
    """Load and clean the example Excel file once for all tests."""
    from excelservices import ExcelService
    excel_path = BACKEND_SRC.parent.parent / "example_sheets" / "Detailed_Expense_Breakdown.xlsx"
    return ExcelService.load_all_sheets(str(excel_path))


@pytest.fixture()
def ctx(cleaned_sheets):
    """Build a mock RunContext with the cleaned data."""
    sheets = cleaned_sheets
    df = next(iter(sheets.values()))
    deps = PipelineDeps(
        sheets=sheets,
        sheet_metas=[],
        original_query="test",
        computed_values={},
        available_fields=list(df.index),
        available_years=list(df.columns),
        user_id="test_user",
    )
    mock_ctx = MagicMock(spec=RunContext)
    mock_ctx.deps = deps
    mock_ctx.model = None
    mock_ctx.retry = 0
    mock_ctx.messages = []
    mock_ctx.tool_name = "retrieve"
    return mock_ctx


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _retrieve_val(ctx, field, year, sheet=""):
    """Retrieve a value and assert it's not an error."""
    result = retrieve(ctx, field, year, sheet)
    assert not result.startswith("ERROR"), f"retrieve('{field}', '{year}') returned: {result}"
    return float(result.split(": ")[-1] if ": " in result else result)


# ---------------------------------------------------------------------------
# Q1: 2019 versus 2020: which year had higher subsidies to private enterprises?
# ---------------------------------------------------------------------------

class TestQ1SubsidiesPrivateEnterprises2019vs2020:
    def test_retrieve_2019(self, ctx):
        val = _retrieve_val(ctx, "To private enterprises", "2019")
        assert val == pytest.approx(620.86, abs=0.01)

    def test_retrieve_2020(self, ctx):
        val = _retrieve_val(ctx, "To private enterprises", "2020")
        assert val == pytest.approx(365.55, abs=0.01)

    def test_answer(self, ctx):
        v2019 = _retrieve_val(ctx, "To private enterprises", "2019")
        v2020 = _retrieve_val(ctx, "To private enterprises", "2020")
        assert v2019 > v2020, "2019 should have higher subsidies to private enterprises"

    def test_sandbox_comparison(self, ctx):
        code = "return 620.86 - 365.55"
        result = _run(ctx,code)
        assert "255" in result, f"Expected ~255.31, got {result}"


# ---------------------------------------------------------------------------
# Q2: Looking at 2018-2022, when was capital expenditure at its lowest?
# ---------------------------------------------------------------------------

class TestQ2CapitalExpenditureLowest2018to2022:
    @pytest.mark.parametrize("year,val", [
        ("2018", 10281.2),
        ("2019", 7225.45),
        ("2020", 9248.63),
        ("2021", 13239.19),
        ("2022", 9520.7),
    ])
    def test_retrieve_each_year(self, ctx, year, val):
        result = _retrieve_val(ctx, "Capital", year)
        assert result == pytest.approx(val, abs=0.01)

    def test_lowest_is_2019(self, ctx):
        years = ["2018", "2019", "2020", "2021", "2022"]
        vals = {y: _retrieve_val(ctx, "Capital", y) for y in years}
        lowest_year = min(vals, key=vals.get)
        assert lowest_year == "2019", f"Expected 2019, got {lowest_year} ({vals})"

    def test_sandbox_min(self, ctx):
        code = "return min(10281.2, 7225.45, 9248.63, 13239.19, 9520.7)"
        result = _run(ctx,code)
        assert "7225" in result, f"Expected 7225.45, got {result}"


# ---------------------------------------------------------------------------
# Q3: In which year did we spend the most on grants to international organizations?
# ---------------------------------------------------------------------------

class TestQ3GrantsToIntlOrganizationsHighest:
    def test_all_years_retrievable(self, ctx):
        years = [str(y) for y in range(2011, 2024)]
        for y in years:
            val = _retrieve_val(ctx, "To international organizations", y)
            assert val >= 0, f"To international organizations {y} = {val}"

    def test_highest_is_2021(self, ctx):
        years = [str(y) for y in range(2011, 2024)]
        vals = {y: _retrieve_val(ctx, "To international organizations", y) for y in years}
        highest_year = max(vals, key=vals.get)
        assert highest_year == "2021", f"Expected 2021, got {highest_year} ({vals[highest_year]})"

    def test_2021_value(self, ctx):
        val = _retrieve_val(ctx, "To international organizations", "2021")
        assert val == pytest.approx(4746.75, abs=0.01)

    def test_sandbox_max(self, ctx):
        code = "return max(1012.1, 1311.46, 1470.15, 1650.64, 1679.11, 1566.49, 1362.99, 1429.36, 1451.82, 1327.4, 4746.75, 3074.03, 1642.38)"
        result = _run(ctx,code)
        assert "4746" in result, f"Expected 4746.75, got {result}"


# ---------------------------------------------------------------------------
# Q4: 2020-2023, highest expenditure on interest to nonresidents?
# ---------------------------------------------------------------------------

class TestQ4InterestToNonresidentsHighest2020to2023:
    @pytest.mark.parametrize("year,val", [
        ("2020", 12042.49),
        ("2021", 12351.9),
        ("2022", 15413.64),
        ("2023", 18560.58),
    ])
    def test_retrieve_each_year(self, ctx, year, val):
        result = _retrieve_val(ctx, "To nonresidents", year)
        assert result == pytest.approx(val, abs=0.01)

    def test_highest_is_2023(self, ctx):
        years = ["2020", "2021", "2022", "2023"]
        vals = {y: _retrieve_val(ctx, "To nonresidents", y) for y in years}
        highest_year = max(vals, key=vals.get)
        assert highest_year == "2023", f"Expected 2023, got {highest_year}"

    def test_sandbox_max(self, ctx):
        code = "return max(12042.49, 12351.9, 15413.64, 18560.58)"
        result = _run(ctx,code)
        assert "18560" in result, f"Expected 18560.58, got {result}"


# ---------------------------------------------------------------------------
# Q5: Were grants to foreign governments higher in 2012 or 2017?
# ---------------------------------------------------------------------------

class TestQ5GrantsToForeignGovs2012vs2017:
    def test_retrieve_2012(self, ctx):
        val = _retrieve_val(ctx, "To foreign governments", "2012")
        assert val == pytest.approx(159.92, abs=0.01)

    def test_retrieve_2017(self, ctx):
        val = _retrieve_val(ctx, "To foreign governments", "2017")
        assert val == pytest.approx(11.48, abs=0.01)

    def test_2012_higher(self, ctx):
        v2012 = _retrieve_val(ctx, "To foreign governments", "2012")
        v2017 = _retrieve_val(ctx, "To foreign governments", "2017")
        assert v2012 > v2017, "2012 should be higher than 2017"

    def test_sandbox_comparison(self, ctx):
        code = "return 159.92 - 11.48"
        result = _run(ctx,code)
        assert "148" in result, f"Expected ~148.44, got {result}"


# ---------------------------------------------------------------------------
# Q6: Average annual expense on grants to foreign governments, 2015-2020?
# ---------------------------------------------------------------------------

class TestQ6AvgGrantsToForeignGovs2015to2020:
    @pytest.mark.parametrize("year,val", [
        ("2015", 0.0),
        ("2016", 0.0),
        ("2017", 11.48),
        ("2018", 0.0),
        ("2019", 0.0),
        ("2020", 0.0),
    ])
    def test_retrieve_each_year(self, ctx, year, val):
        result = _retrieve_val(ctx, "To foreign governments", year)
        assert result == pytest.approx(val, abs=0.01)

    def test_average(self, ctx):
        years = ["2015", "2016", "2017", "2018", "2019", "2020"]
        vals = [_retrieve_val(ctx, "To foreign governments", y) for y in years]
        avg = sum(vals) / len(vals)
        assert avg == pytest.approx(11.48 / 6, abs=0.01)

    def test_sandbox_average(self, ctx):
        code = "return (0.0 + 0.0 + 11.48 + 0.0 + 0.0 + 0.0) / 6"
        result = _run(ctx,code)
        assert "1.91" in result, f"Expected ~1.913, got {result}"


# ---------------------------------------------------------------------------
# Q7: Which year had the highest Subsidies to Private Enterprises?
# ---------------------------------------------------------------------------

class TestQ7HighestSubsidiesToPrivateEnterprises:
    def test_all_years_retrievable(self, ctx):
        years = [str(y) for y in range(2011, 2024)]
        for y in years:
            val = _retrieve_val(ctx, "To private enterprises", y)
            assert val >= 0, f"To private enterprises {y} = {val}"

    def test_highest_is_2011(self, ctx):
        years = [str(y) for y in range(2011, 2024)]
        vals = {y: _retrieve_val(ctx, "To private enterprises", y) for y in years}
        highest_year = max(vals, key=vals.get)
        assert highest_year == "2011", f"Expected 2011, got {highest_year} ({vals[highest_year]})"

    def test_2011_value(self, ctx):
        val = _retrieve_val(ctx, "To private enterprises", "2011")
        assert val == pytest.approx(1848.89, abs=0.01)


# ---------------------------------------------------------------------------
# Q8: How much did interest increase or decrease from 2021 to 2022?
# ---------------------------------------------------------------------------

class TestQ8InterestChange2021to2022:
    def test_retrieve_2021(self, ctx):
        val = _retrieve_val(ctx, "Interest expense", "2021")
        assert val == pytest.approx(35822.3, abs=0.01)

    def test_retrieve_2022(self, ctx):
        val = _retrieve_val(ctx, "Interest expense", "2022")
        assert val == pytest.approx(39624.019, abs=0.01)

    def test_increase(self, ctx):
        v2021 = _retrieve_val(ctx, "Interest expense", "2021")
        v2022 = _retrieve_val(ctx, "Interest expense", "2022")
        change = v2022 - v2021
        assert change > 0, "Interest should increase from 2021 to 2022"
        assert change == pytest.approx(3801.719, abs=0.01)

    def test_sandbox_subtraction(self, ctx):
        code = "return 39624.019 - 35822.3"
        result = _run(ctx,code)
        assert "3801" in result, f"Expected ~3801.72, got {result}"


# ---------------------------------------------------------------------------
# Q9: How did compensation of employees change from 2011 to 2022?
# ---------------------------------------------------------------------------

class TestQ9CompensationChange2011to2022:
    def test_retrieve_2011(self, ctx):
        val = _retrieve_val(ctx, "Compensation of employees", "2011")
        assert val == pytest.approx(78218.52, abs=0.01)

    def test_retrieve_2022(self, ctx):
        val = _retrieve_val(ctx, "Compensation of employees", "2022")
        assert val == pytest.approx(102368.29, abs=0.01)

    def test_increase(self, ctx):
        v2011 = _retrieve_val(ctx, "Compensation of employees", "2011")
        v2022 = _retrieve_val(ctx, "Compensation of employees", "2022")
        change = v2022 - v2011
        assert change > 0, "Compensation should increase from 2011 to 2022"
        assert change == pytest.approx(24149.77, abs=0.01)

    def test_sandbox_subtraction(self, ctx):
        code = "return 102368.29 - 78218.52"
        result = _run(ctx,code)
        assert "24149" in result, f"Expected ~24149.77, got {result}"


# ---------------------------------------------------------------------------
# Q10: How much did expenses to non residents grow from 2019 to 2023?
# ---------------------------------------------------------------------------

class TestQ10NonresidentsGrowth2019to2023:
    def test_retrieve_2019(self, ctx):
        val = _retrieve_val(ctx, "To nonresidents", "2019")
        assert val == pytest.approx(12304.93, abs=0.01)

    def test_retrieve_2023(self, ctx):
        val = _retrieve_val(ctx, "To nonresidents", "2023")
        assert val == pytest.approx(18560.58, abs=0.01)

    def test_growth(self, ctx):
        v2019 = _retrieve_val(ctx, "To nonresidents", "2019")
        v2023 = _retrieve_val(ctx, "To nonresidents", "2023")
        growth = v2023 - v2019
        assert growth > 0, "Nonresidents expenses should grow from 2019 to 2023"
        assert growth == pytest.approx(6255.65, abs=0.01)

    def test_sandbox_subtraction(self, ctx):
        code = "return 18560.58 - 12304.93"
        result = _run(ctx,code)
        assert "6255" in result, f"Expected ~6255.65, got {result}"


# ---------------------------------------------------------------------------
# Data integrity: no -inf values in fields needed by the 10 questions
# ---------------------------------------------------------------------------

class TestNoInfInQuestionFields:
    """Verify that none of the fields referenced by the 10 questions have
    -inf values that would cause null/incomplete results."""

    @pytest.mark.parametrize("field", [
        "To private enterprises",
        "Capital",
        "To international organizations",
        "To nonresidents",
        "To foreign governments",
        "Interest expense",
        "Compensation of employees",
        "Subsidies",
    ])
    def test_no_inf_values(self, cleaned_sheets, field):
        df = next(iter(cleaned_sheets.values()))
        assert field in df.index, f"Field '{field}' not found in data"
        row = df.loc[field]
        inf_count = int(np.isinf(row.values).sum())
        assert inf_count == 0, (
            f"Field '{field}' has {inf_count} -inf values. "
            f"This would cause null/incomplete results."
        )
