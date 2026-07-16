from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from statistics import mean, median, stdev
from typing import Any, Callable

import numpy as np
import pandas as pd
import pydantic_monty
from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from sheet_metadata import SheetMeta


# ============================================================================
# Named Math Operations
# ============================================================================

def op_add(*args: float) -> float:
    return sum(args)

def op_subtract(a: float, b: float) -> float:
    return a - b

def op_multiply(*args: float) -> float:
    result = 1.0
    for a in args:
        result *= a
    return result

def op_divide(a: float, b: float) -> float | None:
    return a / b if b != 0 else None

def op_percentage(a: float, b: float) -> float | None:
    return (a / b) * 100 if b != 0 else None

def op_sqrt(a: float) -> float:
    return math.sqrt(a)

def op_power(a: float, b: float) -> float:
    return math.pow(a, b)

def op_log(a: float, base: float = math.e) -> float | None:
    return math.log(a, base) if a > 0 else None

def op_exp(a: float) -> float:
    return math.exp(a)

def op_abs(a: float) -> float:
    return abs(a)

def op_negate(a: float) -> float:
    return -a

def op_max(*args: float) -> float:
    return max(args)

def op_min(*args: float) -> float:
    return min(args)

def op_average(*args: float) -> float | None:
    return mean(args) if args else None

def op_median(*args: float) -> float | None:
    return median(args) if args else None

def op_yoy_growth(current: float, previous: float) -> float | None:
    return ((current - previous) / previous) * 100 if previous != 0 else None

def op_cagr(end_value: float, start_value: float, num_years: float) -> float | None:
    if start_value <= 0 or num_years <= 0:
        return None
    return ((end_value / start_value) ** (1 / num_years) - 1) * 100

def op_ratio(a: float, b: float) -> float | None:
    return a / b if b != 0 else None

def op_percentage_change(new: float, old: float) -> float | None:
    return ((new - old) / old) * 100 if old != 0 else None

def op_difference(a: float, b: float) -> float:
    return abs(a - b)

def op_stdev(*args: float) -> float | None:
    return stdev(args) if len(args) >= 2 else None

NAMED_OPERATIONS: dict[str, Callable[..., Any]] = {
    "add": op_add,
    "subtract": op_subtract,
    "multiply": op_multiply,
    "divide": op_divide,
    "return_percentage": op_percentage,
    "sqrt": op_sqrt,
    "power": op_power,
    "log": op_log,
    "exp": op_exp,
    "abs": op_abs,
    "negate": op_negate,
    "max": op_max,
    "min": op_min,
    "average": op_average,
    "median": op_median,
    "yoy_growth": op_yoy_growth,
    "cagr": op_cagr,
    "ratio": op_ratio,
    "percentage_change": op_percentage_change,
    "difference": op_difference,
    "stdev": op_stdev,
}

UNARY_OPERATIONS = {"sqrt", "abs", "negate", "exp"}
BINARY_OPERATIONS = {"subtract", "divide", "return_percentage", "power", "log",
                     "yoy_growth", "ratio", "percentage_change", "difference"}
TERNARY_OPERATIONS = {"cagr"}
N_ARY_OPERATIONS = {"add", "multiply", "max", "min", "average", "median", "stdev"}


# ============================================================================
# Dependencies
# ============================================================================

@dataclass
class PipelineDeps:
    """Dependencies passed through RunContext to tools and agents."""
    sheets: dict[str, pd.DataFrame]
    sheet_metas: list[SheetMeta]
    original_query: str
    computed_values: dict[str, Any] = field(default_factory=dict)
    available_fields: list[str] = field(default_factory=list)
    available_years: list[str] = field(default_factory=list)
    user_id: str = "anonymous"

    @property
    def df(self) -> pd.DataFrame:
        """Backward-compatible single-df access: returns the first sheet's df."""
        if self.sheets:
            return next(iter(self.sheets.values()))
        return pd.DataFrame()


# ============================================================================
# Retrieval Tools
# ============================================================================

def retrieve(ctx: RunContext[PipelineDeps], field: str, year: str, sheet: str = "") -> str:
    """Retrieve a numeric value from the financial DataFrame(s).

    If a sheet name is provided, search only that sheet.
    If no sheet name is provided, search across all sheets and return
    all matching values (useful for cross-sheet comparisons in consistent-schema groups).
    """
    # Layer 1: Check result cache before scanning DataFrame
    try:
        from result_cache import build_retrieve_key, result_cache_get, result_cache_set
        cache_key = build_retrieve_key(field, year, sheet)
        cached = result_cache_get(ctx.deps.user_id, cache_key)
        if cached is not None:
            return cached
    except Exception:
        pass  # cache unavailable — proceed with DataFrame scan

    sheets = ctx.deps.sheets
    if not sheets:
        return "ERROR: No sheets available."

    if sheet and sheet in sheets:
        df = sheets[sheet]
        if field in df.index and year in df.columns:
            val = df.loc[field, year]
            result = str(float(val))
        else:
            return f"ERROR: '{field}' or '{year}' not found in sheet '{sheet}'."
    else:
        # Search across all sheets
        results = []
        for sheet_name, df in sheets.items():
            if field in df.index and year in df.columns:
                val = df.loc[field, year]
                results.append(f"{sheet_name}: {float(val)}")

        if results:
            result = "; ".join(results)
        else:
            return f"ERROR: '{field}' or '{year}' not found in any sheet."

    # Layer 1: Store result in cache (skip error returns)
    try:
        from result_cache import build_retrieve_key, result_cache_set
        cache_key = build_retrieve_key(field, year, sheet)
        result_cache_set(ctx.deps.user_id, cache_key, result, cache_type="retrieve")
    except Exception:
        pass

    return result


def extract_val(ctx: RunContext[PipelineDeps], field: str, year: str, sheet: str = "") -> str:
    """Extract a single value from the DataFrame by field and year (optionally from a specific sheet)."""
    return retrieve(ctx, field, year, sheet)


def retrieve_batch(
    ctx: RunContext[PipelineDeps],
    field: str,
    years: list[str],
    sheet: str = "",
) -> str:
    """Retrieve multiple year values for a single field in ONE tool call.

    Optimization 1: collapses N sequential ``retrieve(field, year)`` calls
    (each a separate LLM round-trip) into a single round-trip. For a query
    like "lowest capital expenditure 2018-2022", this saves ~10-32s vs the
    single-value tool.

    Args:
        field: The financial field to look up (e.g. "Capital expenditure").
        years: List of years to retrieve (e.g. ["2018", "2019", "2020"]).
        sheet: Optional sheet name to restrict search.

    Returns a JSON string like ``{"2018": 1500.0, "2019": 1200.0}`` (single-
    sheet mode) or ``{"2018": {"Sheet1": 1500.0, "Sheet2": 1300.0}, ...}``
    (cross-sheet mode). Per-year errors are returned as ``null`` for that key
    so a partial result is still useful.
    """
    if not years:
        return json.dumps({})

    # Per-call cache so repeated batches hit the Layer-1 retrieve cache
    # without re-scanning DataFrames.
    try:
        from result_cache import build_retrieve_key, result_cache_get, result_cache_set
        use_cache = True
    except Exception:
        use_cache = False

    sheets = ctx.deps.sheets
    if not sheets:
        return json.dumps({"error": "No sheets available."})

    result_obj: dict[str, Any] = {}
    if sheet and sheet in sheets:
        df = sheets[sheet]
        for year in years:
            cache_key = build_retrieve_key(field, year, sheet) if use_cache else None
            if use_cache:
                cached = result_cache_get(ctx.deps.user_id, cache_key)
                if cached is not None:
                    try:
                        result_obj[year] = float(cached)
                        continue
                    except (TypeError, ValueError):
                        pass  # fall through to DataFrame scan
            if field in df.index and year in df.columns:
                try:
                    val = float(df.loc[field, year])
                    result_obj[year] = val
                    if use_cache:
                        result_cache_set(ctx.deps.user_id, cache_key, str(val), cache_type="retrieve")
                except (TypeError, ValueError):
                    result_obj[year] = None
            else:
                result_obj[year] = None
    else:
        # Cross-sheet mode: group values by year → {sheet: value}.
        # NOTE: deliberately skip the cache read here. The retrieve cache key
        # ``field_year`` (no sheet) is shared with single-sheet retrievals, so
        # reading from it could return a stale value from a different user's
        # sheet set. Writes are similarly skipped — callers can use
        # sheet-scoped retrievals for cacheable values.
        for year in years:
            matches: dict[str, float] = {}
            for sheet_name, df in sheets.items():
                if field in df.index and year in df.columns:
                    try:
                        matches[sheet_name] = float(df.loc[field, year])
                    except (TypeError, ValueError):
                        continue
            if matches:
                # Single match → scalar; multiple → per-sheet dict.
                result_obj[year] = next(iter(matches.values())) if len(matches) == 1 else matches
            else:
                result_obj[year] = None

    return json.dumps(result_obj)


# ============================================================================
# Sandbox Tool — execute_python_code
# ============================================================================

async def execute_python_code(ctx: RunContext[PipelineDeps], code: str) -> str:
    """
    Execute Python code in a secure sandbox using pydantic-monty.

    Use this tool for ALL calculations after retrieving the needed values.
    The code should define variables for any retrieved values, then use a
    `return` statement to return the final result.

    Example:
        code = '''
        revenue_2022 = 1500000
        expenses_2022 = 800000
        margin = (revenue_2022 - expenses_2022) / revenue_2022 * 100
        return margin
        '''

    Available in the sandbox:
    - Basic Python syntax and operators
    - math module (math.sqrt, math.pow, etc.)
    - Common builtins (abs, round, min, max, sum, len, sorted)
    - NumPy functions (np_mean, np_std, np_median, np_percentile, np_diff,
      np_cumsum, np_min, np_max, np_sum, np_var, np_corrcoef, np_percentile,
      np_round, np_sqrt, np_exp, np_log, np_abs, np_arange, np_linspace,
      np_dot, np_argmax, np_argmin)
    - Pandas functions (pd_series, pd_rolling_mean, pd_rolling_std,
      pd_describe, pd_deduplicate, pd_value_counts, np_histogram)
    """
    import io
    import sys

    # Layer 2: Check sandbox cache before executing
    try:
        from result_cache import sandbox_cache_get, sandbox_cache_set
        cached = sandbox_cache_get(ctx.deps.user_id, code)
        if cached is not None:
            return cached
    except Exception:
        pass

    try:
        # Dedent multi-line code — LLM often generates indented blocks
        # (e.g. inside a triple-quoted string) which pydantic-monty rejects
        # with "Unexpected indentation". textwrap.dedent removes the common
        # leading whitespace from all lines.
        import textwrap
        code = textwrap.dedent(code).strip()

        # Modify code to ensure it has a return statement if it doesn't
        # If code assigns to 'result' or 'result_', add a return statement
        code_stripped = code.strip()
        if not code_stripped.startswith("return"):
            # Check if it assigns to result variable
            if "result " in code or "result=" in code:
                # Extract the last line that assigns to result
                lines = code.split("\n")
                for line in reversed(lines):
                    if "result" in line and ("=" in line or "result" in line):
                        # Convert assignment to return
                        if "=" in line:
                            expr = line.split("=", 1)[1].strip()
                            code = f"return {expr}"
                        else:
                            code = f"return {line}"
                        break

        # Create type definitions for the sandbox
        type_defs = """
import math
from typing import Any

# NumPy functions available in the sandbox
np_mean: Any = None
np_median: Any = None
np_std: Any = None
np_var: Any = None
np_min: Any = None
np_max: Any = None
np_sum: Any = None
np_percentile: Any = None
np_diff: Any = None
np_cumsum: Any = None
np_cumprod: Any = None
np_arange: Any = None
np_linspace: Any = None
np_sqrt: Any = None
np_exp: Any = None
np_log: Any = None
np_abs: Any = None
np_round: Any = None
np_dot: Any = None
np_corrcoef: Any = None
np_argmax: Any = None
np_argmin: Any = None
pd_series: Any = None
pd_rolling_mean: Any = None
pd_rolling_std: Any = None
pd_describe: Any = None
pd_deduplicate: Any = None
pd_value_counts: Any = None
np_histogram: Any = None

# Computed values from the pipeline will be injected
"""

        # Add computed values to type definitions
        for key, value in ctx.deps.computed_values.items():
            if isinstance(value, (int, float)):
                type_defs += f"{key}: float = 0.0\n"
            else:
                type_defs += f"{key}: Any = None\n"

        # Prepare external functions that the sandbox can call.
        # pydantic-monty cannot pass numpy arrays across the sandbox boundary,
        # so we wrap numpy functions to accept Python lists and return Python
        # primitives (floats, lists). This gives the LLM access to numpy's
        # analytical capabilities without exposing the full numpy API.
        external_functions = {
            # NumPy — descriptive statistics
            "np_mean": lambda x: float(np.mean(x)),
            "np_median": lambda x: float(np.median(x)),
            "np_std": lambda x: float(np.std(x)),
            "np_var": lambda x: float(np.var(x)),
            "np_min": lambda x: float(np.min(x)),
            "np_max": lambda x: float(np.max(x)),
            "np_sum": lambda x: float(np.sum(x)),
            "np_percentile": lambda x, q: float(np.percentile(x, q)),
            # NumPy — array operations (return Python lists)
            "np_diff": lambda x: np.diff(x).tolist(),
            "np_cumsum": lambda x: np.cumsum(x).tolist(),
            "np_cumprod": lambda x: np.cumprod(x).tolist(),
            "np_arange": lambda *a: np.arange(*a).tolist(),
            "np_linspace": lambda start, stop, num=50: np.linspace(start, stop, num).tolist(),
            # NumPy — math functions
            "np_sqrt": lambda x: float(np.sqrt(x)),
            "np_exp": lambda x: float(np.exp(x)),
            "np_log": lambda x: float(np.log(x)),
            "np_abs": lambda x: float(np.abs(x)),
            "np_round": lambda x, decimals=0: float(np.round(x, decimals)),
            # NumPy — linear algebra / correlation
            "np_dot": lambda a, b: float(np.dot(a, b)),
            "np_corrcoef": lambda a, b: float(np.corrcoef(a, b)[0, 1]),
            "np_argmax": lambda x: int(np.argmax(x)),
            "np_argmin": lambda x: int(np.argmin(x)),
            # Pandas — time series helpers (return Python lists)
            "pd_series": lambda x: list(x) if not isinstance(x, list) else x,
            "pd_rolling_mean": lambda x, w: pd.Series(x).rolling(window=w).mean().dropna().tolist(),
            "pd_rolling_std": lambda x, w: pd.Series(x).rolling(window=w).std().dropna().tolist(),
            # Pandas — EDA helpers on lists (return JSON-serializable types)
            "pd_describe": lambda x: pd.Series(x).describe().to_dict(),
            "pd_deduplicate": lambda x: list(pd.Series(x).unique()),
            "pd_value_counts": lambda x: pd.Series(x).value_counts().to_dict(),
            "np_histogram": lambda x, bins=10: {"counts": np.histogram(x, bins=bins)[0].tolist(), "bin_edges": np.histogram(x, bins=bins)[1].tolist()},
        }

        # Create the Monty instance
        m = pydantic_monty.Monty(
            code,
            inputs=[],
            script_name="sandbox.py",
            type_check=False,
            type_check_stubs=type_defs,
        )

        # Capture stdout
        old_stdout = sys.stdout
        stdout_capture = io.StringIO()

        try:
            sys.stdout = stdout_capture

            # Run the code directly in the async context
            output = await m.run_async(
                inputs={},
                external_functions=external_functions,
            )

            # Get captured stdout
            stdout_output = stdout_capture.getvalue()

            # Return output from return statement or stdout
            if output is not None:
                result = str(output)
            elif stdout_output:
                result = stdout_output.strip()
            else:
                result = "Code executed successfully (no output)"

            # Layer 2: Store successful sandbox result in cache
            try:
                from result_cache import sandbox_cache_set
                sandbox_cache_set(ctx.deps.user_id, code, result)
            except Exception:
                pass

            return result

        finally:
            sys.stdout = old_stdout

    except Exception as e:
        return f"ERROR: {type(e).__name__}: {str(e)}"


# ============================================================================
# EDA Tools — DataFrame-level exploratory data analysis
# ============================================================================
# These tools have direct access to ctx.deps.sheets (dict[str, pd.DataFrame]).
# They return text/JSON strings that the executor LLM can interpret.
#
# DataFrame layout in this system: fields are the index (rows), years are
# columns. These tools work with any DataFrame layout — they operate on
# columns and rows generically.


def _get_sheet_df(ctx: RunContext[PipelineDeps], sheet_name: str) -> pd.DataFrame | None:
    """Helper: get a DataFrame by sheet name, returning None with an error string."""
    sheets = ctx.deps.sheets
    if not sheets:
        return None
    if sheet_name and sheet_name in sheets:
        return sheets[sheet_name]
    return None


def analyze_sheet(ctx: RunContext[PipelineDeps], sheet_name: str) -> str:
    """Comprehensive EDA summary of a sheet.

    Returns dimensions, describe() output, duplicate count, and per-column
    statistics (unique values, missing percentage, dtype).
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    df = df.copy()
    parts = []
    parts.append(f"=== Sheet: {sheet_name} ===")
    parts.append(f"Dimensions: {df.shape[0]} rows x {df.shape[1]} columns\n")

    parts.append("--- describe() ---")
    parts.append(df.describe(include='all').to_string())
    parts.append("")

    parts.append(f"--- Duplicates ---")
    parts.append(f"Duplicated rows: {df.duplicated().sum()}\n")

    parts.append("--- Per-column stats ---")
    stats_rows = []
    for col in df.columns:
        nunique = df[col].nunique()
        missing_count = df[col].isnull().sum()
        missing_pct = missing_count * 100 / df.shape[0] if df.shape[0] > 0 else 0
        dtype = str(df[col].dtype)
        stats_rows.append({
            "Feature": str(col),
            "Unique_values": nunique,
            "Missing_count": missing_count,
            "Missing_pct": round(missing_pct, 2),
            "Type": dtype,
        })
    stats_df = pd.DataFrame(stats_rows)
    stats_df = stats_df.sort_values("Missing_pct", ascending=False)
    parts.append(stats_df.to_string(index=False))

    return "\n".join(parts)


def find_missing_data(ctx: RunContext[PipelineDeps], sheet_name: str) -> str:
    """Report missing data per column in a sheet.

    Returns absolute missing counts and missing percentage per column.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    parts = []
    parts.append(f"=== Missing Data Report: {sheet_name} ===\n")

    missing_counts = df.isnull().sum()
    missing_pct = df.isnull().sum() / df.shape[0] if df.shape[0] > 0 else 0

    parts.append("--- Missing counts ---")
    for col, count in missing_counts.items():
        if count > 0:
            parts.append(f"  {col}: {count} missing ({missing_pct[col]:.1%})")

    total_missing = missing_counts.sum()
    total_cells = df.shape[0] * df.shape[1]
    parts.append(f"\nTotal missing cells: {total_missing} / {total_cells} ({total_missing/total_cells:.1%})")

    if total_missing == 0:
        parts.append("No missing data found.")

    return "\n".join(parts)


def discover_column_types(ctx: RunContext[PipelineDeps], sheet_name: str) -> str:
    """Classify columns as categorical, discrete numerical, or continuous numerical.

    - Categorical: object dtype columns
    - Discrete: numerical columns with < 20 unique values
    - Continuous: numerical columns with >= 20 unique values
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    df = df.copy()

    categorical = df.select_dtypes(include=object).columns.tolist()
    numerical = df.select_dtypes(include=np.number).columns.tolist()
    discrete = [col for col in numerical if df[col].nunique() < 20]
    continuous = [col for col in numerical if col not in discrete]

    parts = []
    parts.append(f"=== Column Type Discovery: {sheet_name} ===\n")
    parts.append(f"Categorical ({len(categorical)}): {categorical}")
    parts.append(f"Discrete numerical ({len(discrete)}): {discrete}")
    parts.append(f"Continuous numerical ({len(continuous)}): {continuous}")

    return "\n".join(parts)


def get_sheet_shape(ctx: RunContext[PipelineDeps], sheet_name: str) -> str:
    """Return the dimensions (rows x columns) of a sheet."""
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    rows, cols = df.shape
    return f"Sheet '{sheet_name}': {rows} rows x {cols} columns"


def group_summary_stats(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    group_col: str,
    value_col: str,
) -> str:
    """Group by a column and compute summary statistics (sum, mean, median, max, min, std, count).

    Args:
        sheet_name: Name of the sheet to analyze.
        group_col: Column name to group by.
        value_col: Column name to compute statistics on.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    df = df.copy()

    if group_col not in df.columns:
        return f"ERROR: Column '{group_col}' not found. Available columns: {df.columns.tolist()}"
    if value_col not in df.columns:
        return f"ERROR: Column '{value_col}' not found. Available columns: {df.columns.tolist()}"

    if df[value_col].dtype == 'O':
        df[value_col] = pd.to_numeric(df[value_col], errors='coerce')

    try:
        grouped = df.groupby(group_col)[value_col].agg(
            ['count', 'sum', 'mean', 'median', 'std', 'min', 'max']
        ).reset_index()
        return f"=== Group Summary: {sheet_name} (group by {group_col}, stats on {value_col}) ===\n\n{grouped.to_string(index=False)}"
    except Exception as e:
        return f"ERROR: Group summary failed: {type(e).__name__}: {e}"


def create_pivot_table(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    index: str,
    columns: str,
    values: str,
) -> str:
    """Create a pivot table from a sheet.

    Args:
        sheet_name: Name of the sheet.
        index: Column name for pivot index (rows).
        columns: Column name for pivot columns.
        values: Column name for pivot values (aggregated with mean).
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    for col in [index, columns, values]:
        if col not in df.columns:
            return f"ERROR: Column '{col}' not found. Available columns: {df.columns.tolist()}"

    try:
        pivot = df.pivot_table(index=index, columns=columns, values=values, aggfunc='mean', fill_value=0)
        return f"=== Pivot Table: {sheet_name} (index={index}, columns={columns}, values={values}) ===\n\n{pivot.to_string()}"
    except Exception as e:
        return f"ERROR: Pivot table failed: {type(e).__name__}: {e}"


def crosstab_analysis(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    col1: str,
    col2: str,
    aggfunc: str = "",
) -> str:
    """Create a cross-tabulation of two columns.

    Args:
        sheet_name: Name of the sheet.
        col1: First column name (rows of the crosstab).
        col2: Second column name (columns of the crosstab).
        aggfunc: Aggregation function — "mean", "median", "count", "std", or "" (plain count).
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    for col in [col1, col2]:
        if col not in df.columns:
            return f"ERROR: Column '{col}' not found. Available columns: {df.columns.tolist()}"

    try:
        if aggfunc == "mean":
            cross = pd.crosstab(df[col1], df[col2], values=df.get('value', df[col2]), aggfunc=np.mean)
        elif aggfunc == "median":
            cross = pd.crosstab(df[col1], df[col2], values=df.get('value', df[col2]), aggfunc=np.median)
        elif aggfunc == "count":
            cross = pd.crosstab(df[col1], df[col2])
        elif aggfunc == "std":
            cross = pd.crosstab(df[col1], df[col2], values=df.get('value', df[col2]), aggfunc=np.std)
        else:
            cross = pd.crosstab(df[col1], df[col2])

        return f"=== Crosstab: {sheet_name} ({col1} x {col2}, aggfunc={aggfunc or 'count'}) ===\n\n{cross.to_string()}"
    except Exception as e:
        return f"ERROR: Crosstab failed: {type(e).__name__}: {e}"


def drop_missing_columns(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    threshold: float = 0.75,
) -> str:
    """Drop columns with missing data above a threshold percentage.

    Args:
        sheet_name: Name of the sheet.
        threshold: Drop columns where missing data exceeds this fraction (0.0-1.0). Default 0.75.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    df = df.copy()
    row_dim = df.shape[0]
    cols_to_drop = []
    for col in df.columns:
        missing_frac = df[col].isnull().sum() / row_dim if row_dim > 0 else 0
        if missing_frac > threshold:
            cols_to_drop.append(col)

    if cols_to_drop:
        df.drop(columns=cols_to_drop, inplace=True)

    return (
        f"=== Drop Missing Columns: {sheet_name} (threshold={threshold:.0%}) ===\n\n"
        f"Dropped {len(cols_to_drop)} column(s): {cols_to_drop}\n"
        f"New shape: {df.shape[0]} rows x {df.shape[1]} columns"
    )


def convert_to_numerical(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    columns: list[str],
) -> str:
    """Convert specified columns to numerical type, filling NaNs with column mean.

    Args:
        sheet_name: Name of the sheet.
        columns: List of column names to convert from categorical to numerical.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    df = df.copy()
    converted = []
    failed = []

    for col in columns:
        if col not in df.columns:
            failed.append(f"{col} (not found)")
            continue
        try:
            df[col] = pd.to_numeric(df[col], errors='coerce')
            fill_val = df[col].mean()
            if pd.isna(fill_val):
                fill_val = 0.0
            df[col] = df[col].fillna(fill_val)
            converted.append(col)
        except Exception as e:
            failed.append(f"{col} ({e})")

    parts = [
        f"=== Convert to Numerical: {sheet_name} ===\n",
        f"Converted ({len(converted)}): {converted}",
    ]
    if failed:
        parts.append(f"Failed ({len(failed)}): {failed}")
    return "\n".join(parts)


def correlation_analysis(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    columns: list[str] | None = None,
) -> str:
    """Compute a correlation matrix for numerical columns in a sheet.

    Args:
        sheet_name: Name of the sheet.
        columns: Optional list of specific columns to include. If empty, all numerical columns.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    df = df.copy()
    numeric_df = df.select_dtypes(include=np.number)

    if columns:
        available = [c for c in columns if c in numeric_df.columns]
        if not available:
            return f"ERROR: None of {columns} are numerical columns. Numerical columns: {numeric_df.columns.tolist()}"
        numeric_df = numeric_df[available]

    if numeric_df.shape[1] < 2:
        return f"ERROR: Need at least 2 numerical columns for correlation. Found {numeric_df.shape[1]}."

    corr_matrix = numeric_df.corr()
    return f"=== Correlation Matrix: {sheet_name} ===\n\n{corr_matrix.to_string()}"


def get_sheet_head(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    n: int = 5,
) -> str:
    """Return the first N rows of a sheet (like df.head(n)).

    Args:
        sheet_name: Name of the sheet.
        n: Number of rows to return. Default 5.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    n = max(1, min(n, df.shape[0]))
    return f"=== Head ({n} rows): {sheet_name} ===\n\n{df.head(n).to_string()}"


def get_sheet_info(ctx: RunContext[PipelineDeps], sheet_name: str) -> str:
    """Return a summary of a sheet (like df.info()): dtypes, non-null counts, memory usage.

    Args:
        sheet_name: Name of the sheet.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    parts = []
    parts.append(f"=== Sheet Info: {sheet_name} ===\n")
    parts.append(f"RangeIndex: {df.shape[0]} entries, 0 to {df.shape[0] - 1}")
    parts.append(f"Data columns (total {df.shape[1]} columns):\n")

    info_rows = []
    for i, col in enumerate(df.columns):
        non_null = df[col].notna().sum()
        dtype = str(df[col].dtype)
        info_rows.append(f" {i:>3}  {str(col):<30} {non_null:>6} non-null    {dtype}")

    parts.append("\n".join(info_rows))
    parts.append(f"\ndtypes: {dict(df.dtypes.astype(str))}")
    parts.append(f"memory usage: {df.memory_usage(deep=True).sum()} bytes")

    return "\n".join(parts)


def value_counts_analysis(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    column: str,
    normalize: bool = False,
) -> str:
    """Compute value counts for a column (like df[column].value_counts()).

    Args:
        sheet_name: Name of the sheet.
        column: Column name to count values in.
        normalize: If True, return proportions instead of counts.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    if column not in df.columns:
        return f"ERROR: Column '{column}' not found. Available columns: {df.columns.tolist()}"

    vc = df[column].value_counts(normalize=normalize)
    label = "proportion" if normalize else "count"
    return f"=== Value Counts: {sheet_name} / {column} ({label}) ===\n\n{vc.to_string()}"


def deduplicate_rows(
    ctx: RunContext[PipelineDeps],
    sheet_name: str,
    subset: list[str] | None = None,
) -> str:
    """Remove duplicate rows from a sheet and report what was removed.

    Args:
        sheet_name: Name of the sheet.
        subset: Optional list of column names to consider for deduplication.
                If empty, all columns are used.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    original_count = df.shape[0]
    dup_count = df.duplicated(subset=subset if subset else None).sum()

    if subset:
        for col in subset:
            if col not in df.columns:
                return f"ERROR: Column '{col}' not found. Available columns: {df.columns.tolist()}"

    deduped = df.drop_duplicates(subset=subset if subset else None)
    new_count = deduped.shape[0]

    return (
        f"=== Deduplicate: {sheet_name} ===\n\n"
        f"Original rows: {original_count}\n"
        f"Duplicates found: {dup_count}\n"
        f"Rows after dedup: {new_count}\n"
        f"Subset columns: {subset if subset else 'all columns'}"
    )


def get_sheet_dtypes(ctx: RunContext[PipelineDeps], sheet_name: str) -> str:
    """Return the dtype of each column in a sheet.

    Args:
        sheet_name: Name of the sheet.
    """
    df = _get_sheet_df(ctx, sheet_name)
    if df is None:
        return f"ERROR: Sheet '{sheet_name}' not found. Available: {list(ctx.deps.sheets.keys())}"

    dtypes = df.dtypes.astype(str)
    parts = [f"=== Column Dtypes: {sheet_name} ===\n"]
    for col, dtype in dtypes.items():
        parts.append(f"  {col}: {dtype}")
    return "\n".join(parts)


# ============================================================================
# Tool Prepare Functions (dynamic schema customization)
# ============================================================================

async def _prepare_retrieve_tool(
    ctx: RunContext[PipelineDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Inject available fields/years/sheets into the retrieve tool schema."""
    fields = ctx.deps.available_fields
    years = ctx.deps.available_years
    sheet_names = list(ctx.deps.sheets.keys())
    tool_def.parameters_json_schema["properties"]["field"]["description"] = (
        f"Field name from available fields: {', '.join(fields)}"
    )
    tool_def.parameters_json_schema["properties"]["year"]["description"] = (
        f"Year from available years: {', '.join(years)}"
    )
    if "sheet" in tool_def.parameters_json_schema.get("properties", {}):
        tool_def.parameters_json_schema["properties"]["sheet"]["description"] = (
            f"Sheet name (optional). Available sheets: {', '.join(sheet_names)}. "
            f"If omitted, searches all sheets."
        )
    return tool_def


async def _prepare_extract_val_tool(
    ctx: RunContext[PipelineDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Inject available fields/years/sheets into the extract_val tool schema."""
    return await _prepare_retrieve_tool(ctx, tool_def)


async def _prepare_retrieve_batch_tool(
    ctx: RunContext[PipelineDeps], tool_def: ToolDefinition
) -> ToolDefinition | None:
    """Inject available fields/years/sheets into the retrieve_batch tool schema."""
    fields = ctx.deps.available_fields
    years = ctx.deps.available_years
    sheet_names = list(ctx.deps.sheets.keys())
    props = tool_def.parameters_json_schema.get("properties", {})
    if "field" in props:
        props["field"]["description"] = (
            f"Field name from available fields: {', '.join(fields)}"
        )
    if "years" in props:
        props["years"]["description"] = (
            f"List of years from available years: {', '.join(years)}. "
            f"Pass multiple years to fetch them in a single tool call."
        )
        # Allow a small array (most queries want 2-10 years).
        props["years"].setdefault("minItems", 1)
    if "sheet" in props:
        props["sheet"]["description"] = (
            f"Sheet name (optional). Available sheets: {', '.join(sheet_names)}. "
            f"If omitted, searches all sheets."
        )
    return tool_def
