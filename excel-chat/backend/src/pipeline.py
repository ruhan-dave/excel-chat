from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from statistics import mean, median, stdev
from typing import Any, Callable, Literal

import pandas as pd
import pydantic_monty
from pydantic import BaseModel, Field, field_validator
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.tools import ToolDefinition

from llama_index.core.prompts import PromptTemplate

from sheet_metadata import SheetMeta
from guardrails import GUARDRAIL_SYSTEM_PROMPT, inject_disclaimer


# ============================================================================
# Timing Instrumentation (Optimization 6)
# ============================================================================

@contextmanager
def timed(stage: str, timings: dict[str, float]):
    """Record elapsed wall-clock time for ``stage`` into ``timings``.

    Usage:
        timings: dict[str, float] = {}
        with timed("planner", timings):
            ...
        # timings["planner"] now holds the elapsed seconds.

    Failures inside the ``with`` block propagate normally; the elapsed time is
    recorded either way so a slow failed stage is still visible in logs.
    """
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        timings[stage] = timings.get(stage, 0.0) + elapsed
        print(f"⏱️  {stage}: {elapsed:.2f}s")


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
# Structured Output Models
# ============================================================================

class PlanStep(BaseModel):
    """A single step in the execution plan."""
    action: Literal[
        "retrieve", "retrieve_batch", "compute",
        "add", "subtract", "multiply", "divide", "return_percentage",
        "sqrt", "power", "log", "exp", "abs", "negate",
        "max", "min", "average", "median", "stdev",
        "yoy_growth", "cagr", "ratio", "percentage_change", "difference",
    ] = Field(
        description=(
            "The action to perform. "
            "'retrieve' fetches a single value from the DataFrame. "
            "'retrieve_batch' fetches multiple year values for one field in ONE call "
            "(preferred when you need 2+ years for the same field — saves LLM round-trips). "
            "'compute' runs arbitrary Python code in a secure sandbox for complex calculations. "
            "Named operations (add, subtract, multiply, divide, return_percentage, sqrt, power, "
            "log, exp, abs, negate, max, min, average, median, stdev, yoy_growth, cagr, ratio, "
            "percentage_change, difference) apply the corresponding math function to prior step results."
        )
    )
    args: list[str] = Field(
        description=(
            "For 'retrieve': ['FieldName', 'Year'] to search all sheets, "
            "or ['SheetName', 'FieldName', 'Year'] to search a specific sheet. "
            "For 'retrieve_batch': ['FieldName', 'Year1', 'Year2', ...] for cross-sheet, "
            "or ['SheetName', 'FieldName', 'Year1', 'Year2', ...] for a specific sheet. "
            "For 'compute': a natural-language description of the calculation. "
            "For named operations: references to prior steps (e.g. ['step1', 'step2']) "
            "or literal numbers (e.g. ['step1', '100']). "
            "Unary ops (sqrt, abs, negate, exp): one arg. "
            "Binary ops (subtract, divide, return_percentage, power, log, yoy_growth, ratio, "
            "percentage_change, difference): two args. "
            "Ternary ops (cagr): three args [end_value, start_value, num_years]. "
            "N-ary ops (add, multiply, max, min, average, median, stdev): two or more args."
        )
    )


class QueryPlan(BaseModel):
    """Structured plan generated by the planner agent."""
    task_type: Literal[
        "retrieve_numbers", "perform_calculations", "give_advice", "other"
    ] = Field(description="The type of task the user is asking for.")
    plan: dict[str, PlanStep] | None = Field(
        default=None,
        description="Numbered steps for 'perform_calculations' tasks.",
    )
    items: list[str] | None = Field(
        default=None,
        description="List of 'FieldName, Year' strings for 'retrieve_numbers' tasks.",
    )
    description: str | None = Field(
        default=None,
        description="Description of advice needed for 'give_advice' tasks.",
    )

    @field_validator("plan", mode="before")
    @classmethod
    def _parse_plan(cls, v):
        if isinstance(v, list):
            converted = {}
            for i, item in enumerate(v, 1):
                if isinstance(item, dict):
                    if "step" in item and isinstance(item["step"], dict):
                        converted[f"step{i}"] = item["step"]
                    else:
                        converted[f"step{i}"] = item
                else:
                    converted[f"step{i}"] = item
            return converted
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
                if isinstance(parsed, list):
                    return cls._parse_plan(parsed)
            except (json.JSONDecodeError, TypeError):
                pass
        return v

    @field_validator("items", mode="before")
    @classmethod
    def _parse_items(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass
        return v


def _flatten_value(v: Any) -> Any:
    """Flatten dict/list values to readable strings so the frontend doesn't show [object Object]."""
    if isinstance(v, dict):
        if "value" in v and len(v) == 1:
            return v["value"]
        return json.dumps(v, default=str)
    if isinstance(v, list):
        return json.dumps(v, default=str)
    return v


class ExecutionResult(BaseModel):
    """Result of executing a query plan."""
    step_results: dict[str, Any] = Field(
        default_factory=dict,
        description="Results from each executed step.",
    )
    final_answer: Any = Field(
        description="The final computed or retrieved answer.",
    )
    explanation: str = Field(
        default="",
        description="Brief explanation of how the answer was derived.",
    )
    friendly_response: str = Field(
        default="",
        description="A clear, natural language response to the user's question. "
        "Include specific numerical values with proper formatting (currency, percentages). "
        "Briefly explain how the answer was derived. Use a friendly, professional tone.",
    )

    @field_validator("final_answer", "step_results", mode="before")
    @classmethod
    def _flatten_nested_objects(cls, v):
        if isinstance(v, dict):
            return {k: _flatten_value(val) for k, val in v.items()}
        return _flatten_value(v)


class FriendlyResponse(BaseModel):
    """User-friendly natural language response."""
    response: str = Field(description="The conversational response to the user.")


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
# Model Factory
# ============================================================================

def build_openrouter_model() -> OpenAIModel:
    """Create an OpenAIModel configured for OpenRouter."""
    return OpenAIModel(
        model_name=os.environ.get("MODEL_ID", "openai/gpt-oss-120b:nitro"),
        base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        api_key=os.environ.get("OPENROUTER_API_KEY"),
    )


# ============================================================================
# Tools
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

# Computed values from the pipeline will be injected
"""
        
        # Add computed values to type definitions
        for key, value in ctx.deps.computed_values.items():
            if isinstance(value, (int, float)):
                type_defs += f"{key}: float = 0.0\n"
            else:
                type_defs += f"{key}: Any = None\n"
        
        # Prepare external functions that the sandbox can call
        external_functions = {}
        
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


# ============================================================================
# Agent Builders
# ============================================================================

def build_planner_agent(sheets: dict[str, pd.DataFrame], sheet_metas: list[SheetMeta]) -> Agent[None, QueryPlan]:
    """Agent that generates a structured QueryPlan from a user question."""
    model = build_openrouter_model()

    # Build multi-sheet context
    sheet_context_parts = []
    all_fields = set()
    all_years = set()
    for meta in sheet_metas:
        all_fields.update(meta.fields)
        all_years.update(meta.years)
        group_note = f" (schema group: {meta.schema_group})" if meta.schema_group and meta.schema_group != "unique" else ""
        desc_note = f"\n      Description: {meta.combined_description}" if meta.combined_description != "No description available." else ""
        sheet_context_parts.append(
            f"  - Sheet '{meta.sheet_name}' (file: {meta.file_name}){group_note}\n"
            f"      Fields: {', '.join(meta.fields[:20])}\n"
            f"      Years: {', '.join(meta.years)}{desc_note}"
        )
    sheet_context = "\n".join(sheet_context_parts)
    fields_str = ", ".join(sorted(all_fields))
    years_str = ", ".join(sorted(all_years))

    # Identify schema groups for cross-sheet note
    groups = {}
    for meta in sheet_metas:
        if meta.schema_group and meta.schema_group != "unique":
            groups.setdefault(meta.schema_group, []).append(meta.sheet_name)
    cross_sheet_note = ""
    if groups:
        group_descs = [f"  - {', '.join(sheets_list)}" for sheets_list in groups.values()]
        cross_sheet_note = f"\n    Cross-sheet calculations are possible for sheets with the same schema group:\n{chr(10).join(group_descs)}\n    When retrieving from a schema group, omit the sheet name to search all sheets in that group."

    instructions = f"""You are a financial data analysis planner.

    Available sheets and their data:
{sheet_context}

    All available fields across sheets: {fields_str}
    All available years across sheets: {years_str}
{cross_sheet_note}

    Task types:
    - retrieve_numbers: Simple lookups (e.g., "What was revenue in 2022?")
    - perform_calculations: Any math or comparison (e.g., "What's the profit margin?", "Compare YoY growth")
    - give_advice: Recommendations or analysis
    - other: Everything else

    For retrieve_numbers: return items like ["Revenue, 2022"] or ["SheetName, Revenue, 2022"] for sheet-specific retrieval.
    For perform_calculations: return a plan using these step types:

    1. retrieve — fetch a single value: args = ["FieldName", "Year"] (searches all sheets) or ["SheetName", "FieldName", "Year"] (specific sheet)
    2. retrieve_batch — fetch multiple year values for ONE field in a single step (PREFERRED when 2+ years needed for the same field — saves executor LLM round-trips):
       args = ["FieldName", "Year1", "Year2", ...] for cross-sheet, or
              ["SheetName", "FieldName", "Year1", "Year2", ...] for a specific sheet
       Example: step1: retrieve_batch ["Capital expenditure", "2018", "2019", "2020", "2021", "2022"]
    3. Named math operations — apply to prior step results or literal numbers:
       - Unary (1 arg): sqrt, abs, negate, exp
       - Binary (2 args): subtract, divide, return_percentage, power, log, yoy_growth, ratio, percentage_change, difference
       - Ternary (3 args): cagr [end_value, start_value, num_years]
       - N-ary (2+ args): add, multiply, max, min, average, median, stdev
       Args can be step references (e.g. "step1") or literal numbers (e.g. "100").
    3. compute — for complex multi-step calculations that can't be expressed with named ops,
       provide a natural-language description of the calculation as args[0].
       The executor will translate this into Python code and run it in a sandbox.

    Prefer named operations for simple math. Use compute for complex formulas,
    multi-step calculations, or when you need math module functions not covered above.

    When a query involves data from multiple sheets (especially in the same schema group),
    use retrieve with specific sheet names to get values from each sheet, then apply
    named operations or compute to combine them.

    Examples:

    [Simple percentage - single sheet]
    step1: retrieve ["Wages and salaries", "2022"]
    step2: retrieve ["Expense", "2022"]
    step3: return_percentage ["step1", "step2"]

    [YoY growth]
    step1: retrieve ["Revenue", "2023"]
    step2: retrieve ["Revenue", "2022"]
    step3: yoy_growth ["step1", "step2"]

    [Cross-sheet comparison - same schema group]
    step1: retrieve ["Sheet1", "Revenue", "2022"]
    step2: retrieve ["Sheet2", "Revenue", "2022"]
    step3: subtract ["step1", "step2"]

    [Cross-sheet average across all sheets]
    step1: retrieve ["Revenue", "2022"]  (returns values from all matching sheets)
    step2: compute ["Calculate the average of all Revenue 2022 values retrieved in step1"]

    [CAGR over 5 years]
    step1: retrieve ["Revenue", "2023"]
    step2: retrieve ["Revenue", "2018"]
    step3: cagr ["step1", "step2", "5"]

    [Multi-year batch (preferred over N retrievals)]
    step1: retrieve_batch ["Capital expenditure", "2018", "2019", "2020", "2021", "2022"]
    step2: compute ["Find the year with the lowest capital expenditure among step1"]

    [Complex calculation via compute]
    step1: retrieve ["Revenue", "2023"]
    step2: retrieve ["Revenue", "2021"]
    step3: retrieve ["Expenses", "2023"]
    step4: compute ["Calculate the compound annual growth rate of Revenue from 2021 to 2023, then multiply by the 2023 profit margin (Revenue - Expenses) / Revenue"]

    Always use exact field names and years from the available lists.
    {GUARDRAIL_SYSTEM_PROMPT}
    """

    return Agent(
        model,
        result_type=QueryPlan,
        system_prompt=instructions,
        model_settings={"temperature": 0.1},
        result_retries=2,
    )


def build_executor_agent(sheets: dict[str, pd.DataFrame], sheet_metas: list[SheetMeta]) -> Agent[PipelineDeps, ExecutionResult]:
    """Agent that executes a plan using tools, with structured output."""
    model = build_openrouter_model()

    retrieve_t = Tool(retrieve, prepare=_prepare_retrieve_tool)
    extract_t = Tool(extract_val, prepare=_prepare_extract_val_tool)
    retrieve_batch_t = Tool(retrieve_batch, prepare=_prepare_retrieve_batch_tool)

    sheet_names = list(sheets.keys())
    system_prompt = f"""You are a financial data execution engine.

    You have access to {len(sheets)} sheet(s): {', '.join(sheet_names)}

    You have three tools:
    1. retrieve / extract_val — fetch a SINGLE value for a (field, year) pair
       - If a sheet name is provided, searches only that sheet.
       - If no sheet name is provided, searches all sheets and returns all matching values.
    2. retrieve_batch — fetch MULTIPLE year values for ONE field in a single tool call.
       PREFER THIS TOOL when you need 2+ years for the same field — each call to
       retrieve is a separate LLM round-trip, but retrieve_batch collapses them
       into one. For example, instead of:
           retrieve("Capital", "2018"); retrieve("Capital", "2019"); retrieve("Capital", "2020");
       do:
           retrieve_batch("Capital", ["2018", "2019", "2020"])
       Returns a JSON object like {{"2018": 1500.0, "2019": 1200.0}} for single-sheet
       mode, or {{"2018": {{"Sheet1": 1500.0, "Sheet2": 1300.0}}}} for cross-sheet mode.
    3. execute_python_code — run Python code in a secure sandbox to perform ANY calculation

    The sandbox supports:
    - Basic Python syntax and operators (+, -, *, /, **, //, %)
    - math module (math.sqrt, math.pow, math.log, math.exp, math.ceil, math.floor, etc.)
    - Common builtins (abs, round, min, max, sum, len, sorted)
    - statistics module (statistics.mean, statistics.median, statistics.stdev, etc.)

    Execution flow:
    1. Call retrieve_batch (preferred when you need multiple years for one field)
       or retrieve (for single values) to get all needed values from the plan
    2. For named operations (add, subtract, multiply, divide, return_percentage, sqrt, power,
       log, exp, abs, negate, max, min, average, median, stdev, yoy_growth, cagr, ratio,
       percentage_change, difference): either compute directly in Python or use execute_python_code
    3. For compute steps: call execute_python_code with Python code that performs the full
       calculation using the retrieved values as literal numbers
    4. Return the final answer as structured data.

    ### CRITICAL — step_results MUST be populated:
    The `step_results` field in your output MUST contain an entry for EVERY step in the plan,
    including pre-populated steps. For each step key (e.g. "step1", "step2"), set its value
    to the result of that step. For retrieve/retrieve_batch steps, use the retrieved value(s).
    For named operations and compute steps, use the computed result.
    Example: if the plan has step1 (retrieve_batch), step2 (retrieve_batch), step3 (compute),
    then step_results should be: {{"step1": {{...}}, "step2": {{...}}, "step3": <computed_value>}}.
    Do NOT leave step_results as an empty dict {{}}.

    ### CRITICAL — friendly_response MUST use actual field names:
    When writing the `friendly_response` field, ALWAYS refer to the actual field names from
    the plan — never use generic phrases like "the first series" or "the second value".
    For example, instead of "The first benefit series grew at 25.4%", write
    "Social security benefits grew at a CAGR of 25.4%".
    The field names are provided in the execution prompt alongside each step.

    When retrieve returns multiple values (from searching all sheets), parse them carefully.
    The format is "SheetName: value; SheetName2: value2".

    When writing Python code for execute_python_code:
    - Use the retrieved numeric values directly as literals in the code
    - Use a `return` statement to return the final result
    - Example: code = 'revenue = 1500000\\nexpenses = 800000\\nmargin = (revenue - expenses) / revenue * 100\\nreturn margin'

    If a retrieval fails, note it and continue with what you can.
    {GUARDRAIL_SYSTEM_PROMPT}
    """

    return Agent(
        model,
        deps_type=PipelineDeps,
        result_type=ExecutionResult,
        system_prompt=system_prompt,
        tools=[
            retrieve_t,
            extract_t,
            retrieve_batch_t,
            execute_python_code,
        ],
        model_settings={"temperature": 0.1},
        result_retries=2,
    )


def build_responder_agent() -> Agent[None, str]:
    """Agent that generates a friendly natural language response."""
    model = build_openrouter_model()

    return Agent(
        model,
        result_type=str,
        system_prompt=f"""You are a helpful financial assistant.

        Given a user's question and the calculated results, provide a clear, conversational
        response that directly answers the question. Include specific numerical values
        with proper formatting (currency, percentages). Briefly explain how the answer
        was derived. Use a friendly, professional tone.
        {GUARDRAIL_SYSTEM_PROMPT}
        """,
        model_settings={"temperature": 0.3},
    )


def _format_simple_response(query: str, plan: QueryPlan, execution: ExecutionResult) -> str | None:
    """Generate a deterministic response for simple queries without calling the LLM.

    Returns None if the query is complex enough to warrant the responder agent.
    Handles:
    - Single retrieve_numbers (1 item): "Revenue in 2022 was $1,234,567"
    - Simple named operations with explanation: "The sum is $2,000"
    """
    step_results = execution.step_results or {}
    final = execution.final_answer
    explanation = execution.explanation or ""

    # Case 1: retrieve_numbers with 1-2 items
    if plan.task_type == "retrieve_numbers" and plan.items:
        if len(plan.items) <= 2:
            parts = []
            for item in plan.items:
                fields = [f.strip() for f in item.split(",")]
                if len(fields) == 2:
                    field_name, year = fields
                    val = final if len(plan.items) == 1 else step_results.get(f"item_{fields[0]}_{fields[1]}", final)
                    parts.append(f"{field_name} in {year}: {val}")
                elif len(fields) == 3:
                    sheet_name, field_name, year = fields
                    parts.append(f"{field_name} in {year} ({sheet_name}): {final}")
            if parts:
                return "  |  ".join(parts)

    # Case 2: single-step plan with a clear explanation
    if plan.plan and len(plan.plan) == 1 and explanation:
        step_name = list(plan.plan.keys())[0]
        step = plan.plan[step_name]
        val = step_results.get(step_name, final)
        if step.action == "retrieve":
            return f"{explanation}: {val}"
        # Simple named ops with explanation
        if val is not None and explanation:
            return f"{explanation}: {val}"

    return None


# ============================================================================
# Optimization 2: Pre-populate retrievals in pure Python (no LLM)
# ============================================================================

def _looks_like_year(s: str) -> bool:
    """Return True if ``s`` parses as a 4-digit year (e.g. "2022", "2022.0")."""
    if not s:
        return False
    s = s.strip()
    if len(s) == 4 and s.isdigit() and 1900 <= int(s) <= 2100:
        return True
    if s.endswith(".0") and s[:-2].isdigit() and 1900 <= int(s[:-2]) <= 2100:
        return True
    return False


def _prepopulate_retrievals(plan: QueryPlan, deps: PipelineDeps) -> dict[str, Any]:
    """Execute every retrieve / retrieve_batch step in pure Python (no LLM).

    Optimization 2: collapses N sequential LLM round-trips into a single
    Python pass before the executor runs. The executor then only handles
    computation + friendly-response generation.

    The returned dict maps step name → retrieved value (a scalar string from
    ``retrieve`` or a parsed dict from ``retrieve_batch``). Per-step failures
    are captured as ``"ERROR: ..."`` strings so downstream code can decide.
    """
    from types import SimpleNamespace

    pre_populated: dict[str, Any] = {}
    if not plan.plan:
        return pre_populated

    # ``retrieve`` and ``retrieve_batch`` only touch ``ctx.deps`` — a
    # SimpleNamespace is sufficient.
    ctx = SimpleNamespace(deps=deps)

    for name, step in plan.plan.items():
        args = step.args or []
        try:
            if step.action == "retrieve":
                if len(args) == 2:
                    field, year = args
                    pre_populated[name] = retrieve(ctx, field, year)
                elif len(args) == 3:
                    sheet, field, year = args
                    pre_populated[name] = retrieve(ctx, field, year, sheet)
                else:
                    pre_populated[name] = f"ERROR: retrieve expects 2 or 3 args, got {len(args)}"
            elif step.action == "retrieve_batch":
                # Cross-sheet: ["Field", "Year1", "Year2", ...] → args[1] is a year
                # Specific sheet: ["Sheet", "Field", "Year1", ...] → args[1] is the field
                if len(args) < 2:
                    pre_populated[name] = "ERROR: retrieve_batch needs at least 2 args"
                    continue
                if _looks_like_year(args[1]):
                    field = args[0]
                    years = args[1:]
                    raw = retrieve_batch(ctx, field, years, sheet="")
                else:
                    sheet = args[0]
                    field = args[1]
                    years = args[2:]
                    raw = retrieve_batch(ctx, field, years, sheet=sheet)
                try:
                    pre_populated[name] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    pre_populated[name] = raw
            else:
                # Named ops / compute can't be pre-populated — leave for executor.
                continue
        except Exception as e:
            print(f"⚠️ Pre-populate failed for {name}: {e}")
            pre_populated[name] = f"ERROR: {type(e).__name__}: {e}"

    return pre_populated


def _format_pre_populated_for_prompt(pre_populated: dict[str, Any]) -> str:
    """Render pre-populated values as a human-readable block for the executor.

    Format:
        Pre-computed values (use these directly, do NOT call retrieve):

        step1: 1500.0
        step2: 1200.0
        step3: {"2018": 1500.0, "2019": 1200.0}
    """
    if not pre_populated:
        return ""
    lines = [
        "Pre-computed values (use these directly — do NOT call retrieve or "
        "retrieve_batch again):"
    ]
    for name, value in pre_populated.items():
        lines.append(f"  {name}: {value}")
    return "\n".join(lines)


def _plan_is_pure_retrieve(plan: QueryPlan) -> bool:
    """True iff every step is a retrieve/retrieve_batch (no compute, no named ops)."""
    if not plan.plan:
        return False
    return all(
        step.action in {"retrieve", "retrieve_batch"}
        for step in plan.plan.values()
    )


# ============================================================================
# Public Pipeline API
# ============================================================================

def build_query_pipeline(
    llm_client: Any | None,
    sheets: dict[str, pd.DataFrame],
    sheet_metas: list[SheetMeta],
    classification_template: PromptTemplate,
    user_id: str = "anonymous",
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    """
    Build a Pydantic AI agent pipeline for querying financial data across multiple sheets.

    Returns a callable that accepts a query string and returns the pipeline result.
    The llm_client argument is kept for backward compatibility but ignored;
    agents create their own OpenRouter model internally.
    """
    all_fields = sorted(set(f for meta in sheet_metas for f in meta.fields))
    all_years = sorted(set(y for meta in sheet_metas for y in meta.years))

    def _emit(event_type: str, data: dict[str, Any]) -> None:
        if on_event:
            try:
                on_event(event_type, data)
            except Exception:
                pass

    async def run_pipeline(query: str) -> dict:
        # Optimization 6: per-stage timing dict returned alongside the answer.
        timings: dict[str, float] = {}

        # Step 1: Plan
        _emit("status", {"message": "Analyzing your question…"})
        planner = build_planner_agent(sheets, sheet_metas)
        with timed("planner", timings):
            plan_result = await planner.run(query)
        plan: QueryPlan = plan_result.data
        print("📋 Plan:", json.dumps(plan.model_dump(), indent=2))

        # Emit classified intent + plan
        _emit("plan", {
            "task_type": plan.task_type,
            "plan": plan.model_dump().get("plan"),
            "items": plan.items,
            "description": plan.description,
        })

        # Step 2: Execute
        deps = PipelineDeps(
            sheets=sheets,
            sheet_metas=sheet_metas,
            original_query=query,
            available_fields=all_fields,
            available_years=all_years,
            user_id=user_id,
        )

        # ------------------------------------------------------------------
        # Optimization 2: pre-populate retrieve / retrieve_batch steps in pure
        # Python. The executor then only has to handle compute / friendly
        # response, which collapses the LLM round-trip count.
        # ------------------------------------------------------------------
        pre_populated: dict[str, Any] = {}
        if plan.plan:
            _emit("status", {"message": "Retrieving data from sheets…"})
            with timed("pre_populate", timings):
                pre_populated = _prepopulate_retrievals(plan, deps)
            if pre_populated:
                print(f"⚡ Pre-populated {len(pre_populated)} values: {pre_populated}")
                _emit("pre_populated", {"values": pre_populated})

        # ------------------------------------------------------------------
        # Short-circuit: if every step is a retrieve/retrieve_batch, we don't
        # need the executor at all. Build the ExecutionResult directly.
        # ------------------------------------------------------------------
        if plan.plan and _plan_is_pure_retrieve(plan):
            step_results: dict[str, Any] = {}
            final_answers: list[Any] = []
            for name, step in plan.plan.items():
                val = pre_populated.get(name)
                step_results[name] = val
                if not str(val).startswith("ERROR"):
                    final_answers.append(val)
            # Single value → scalar; multiple → list.
            final_answer: Any = (
                final_answers[0] if len(final_answers) == 1 else final_answers
            )
            execution = ExecutionResult(
                step_results=step_results,
                final_answer=final_answer,
                explanation=(
                    f"Retrieved {len(final_answers)} value(s) directly from the "
                    "DataFrame (no executor LLM call required)."
                ),
                friendly_response="",  # filled in by _format_simple_response
            )
            _emit("status", {"message": "All values retrieved — preparing answer…"})
            print("⚡ Skipped executor — pure retrieve plan.")
            try:
                from result_cache import cache_step_results
                cache_step_results(user_id, plan, execution)
            except Exception as e:
                print(f"⚠️ Post-execution caching failed: {e}")
            friendly = inject_disclaimer(_format_simple_response(query, plan, execution) or "")
            if friendly:
                print(f"📝 Response: {friendly[:100]}...")
            _emit("friendly", {"response": friendly})
            total = sum(timings.values())
            print(f"⏱️  Pipeline total: {total:.2f}s | {timings}")
            _emit("done", {"timings": timings, "total": total})
            return {
                "answer": execution.model_dump(),
                "friendly_response": friendly,
                "timings": timings,
            }

        _emit("status", {"message": "Running calculations…"})
        executor = build_executor_agent(sheets, sheet_metas)

        # Build execution prompt from the plan
        if plan.task_type == "retrieve_numbers" and plan.items:
            exec_prompt = (
                f"Retrieve the following values and return them as the final answer: {plan.items}"
            )
        elif plan.plan:
            retrieve_steps = []
            named_steps = []
            compute_desc = ""
            # Build a step-to-field-name mapping so the executor knows which
            # field each step refers to (for use in friendly_response).
            step_field_map: list[str] = []
            for name, step in plan.plan.items():
                if step.action in {"retrieve", "retrieve_batch"}:
                    # Extract field name from args for context
                    if step.action == "retrieve_batch":
                        if _looks_like_year(step.args[1] if len(step.args) > 1 else ""):
                            field_name = step.args[0] if step.args else ""
                        else:
                            field_name = step.args[1] if len(step.args) > 1 else ""
                    else:
                        field_name = step.args[0] if step.args else ""
                    step_field_map.append(f"  {name} → field: {field_name}")
                    # If we already pre-populated this step, tell the executor
                    # to use the literal value instead of re-fetching.
                    if name in pre_populated:
                        retrieve_steps.append(
                            f"  {name} (field: {field_name}): ALREADY DONE — value is {pre_populated[name]}"
                        )
                    else:
                        verb = "retrieve_batch" if step.action == "retrieve_batch" else "retrieve"
                        retrieve_steps.append(f"  {name} (field: {field_name}): {verb}({step.args})")
                elif step.action == "compute":
                    compute_desc = step.args[0] if step.args else ""
                elif step.action in NAMED_OPERATIONS:
                    named_steps.append(f"  {name}: {step.action}({step.args})")
            steps_desc = "\n".join(retrieve_steps)
            named_desc = "\n".join(named_steps)
            pre_populated_block = _format_pre_populated_for_prompt(pre_populated)
            parts = []
            if pre_populated_block:
                parts.append(pre_populated_block)
            if retrieve_steps:
                parts.append(f"Retrieve these values:\n{steps_desc}")
            if named_steps:
                parts.append(f"Apply these named operations:\n{named_desc}")
            if compute_desc:
                parts.append(f"Then use execute_python_code to calculate: {compute_desc}")
            # Include step-to-field mapping so executor can name fields in friendly_response
            if step_field_map:
                parts.append(
                    "Step-to-field mapping (use these field names in your friendly_response):\n"
                    + "\n".join(step_field_map)
                )
            parts.append(f"User query: {query}")
            exec_prompt = "\n\n".join(parts)
        else:
            exec_prompt = query

        with timed("executor", timings):
            exec_result = await executor.run(exec_prompt, deps=deps)
        execution: ExecutionResult = exec_result.data
        print("✅ Execution:", json.dumps(execution.model_dump(), indent=2))
        _emit("execution", {"step_results": execution.step_results, "final_answer": execution.final_answer, "explanation": execution.explanation})

        # Layer 3: Post-execution structured key derivation
        try:
            from result_cache import cache_step_results
            cache_step_results(user_id, plan, execution)
        except Exception as e:
            print(f"⚠️ Post-execution caching failed: {e}")

        # Step 3: Use executor's friendly_response (merged responder)
        friendly = inject_disclaimer(
            execution.friendly_response or _format_simple_response(query, plan, execution) or ""
        )
        if friendly:
            print(f"📝 Response: {friendly[:100]}...")
        else:
            print("⚠️ No friendly response generated")
        _emit("friendly", {"response": friendly})

        total = sum(timings.values())
        print(f"⏱️  Pipeline total: {total:.2f}s | {timings}")
        _emit("done", {"timings": timings, "total": total})
        return {
            "answer": execution.model_dump(),
            "friendly_response": friendly,
            "timings": timings,
        }

    return run_pipeline


async def generate_user_friendly_response(
    llm_client: Any | None, original_query: str, json_result: Any
) -> str:
    """
    Generate a user-friendly response from the JSON result.
    llm_client is kept for backward compatibility but ignored.
    """
    responder = build_responder_agent()
    prompt = f"""User's Question: {original_query}

    Calculated Results:
    {json.dumps(json_result, indent=2)}

    Provide a clear, natural language response."""
    result = await responder.run(prompt)
    return inject_disclaimer(result.data)


# ============================================================================
# Legacy helper functions (preserved for backward compatibility)
# ============================================================================

def executing_plan_from_json(df: pd.DataFrame, json_str: str) -> dict[str, Any]:
    """
    Legacy deterministic plan executor. Kept for backward compatibility.
    """
    try:
        parsed = json.loads(json_str)
        print("🔨 Parsed JSON:", parsed)

        if "plan" in parsed:
            plan = parsed["plan"]
        elif "items" in parsed:
            plan = {
                f"step{i + 1}": {"action": "retrieve", "args": item.split(",")}
                for i, item in enumerate(parsed["items"])
            }
        else:
            raise ValueError("Invalid JSON format - missing 'plan' or 'items'")

        context: dict[str, Any] = {}
        computed_values: dict[str, Any] = {}

        for step_name, instruction in plan.items():
            print(f"🔧 Processing step {step_name}: {instruction}")

            if isinstance(instruction, str):
                instruction = instruction.strip()
                if instruction.lower().startswith("retrieve"):
                    args_part = instruction[8:].strip().strip("[]'\"")
                    parts = [p.strip().strip("'\"") for p in args_part.split(",")]
                    if len(parts) >= 2:
                        col, year = parts[0], parts[1]
                        values = _legacy_retrieving(df, [col, year])
                        context[step_name] = values[0] if values else None
                        computed_values[step_name] = context[step_name]
                continue

            if isinstance(instruction, dict):
                action = instruction.get("action", "").lower()
                args = instruction.get("args", [])

                normalized_args = []
                for arg in args:
                    if isinstance(arg, str) and "," in arg:
                        normalized_args.extend([x.strip() for x in arg.split(",")])
                    else:
                        normalized_args.append(arg.strip() if isinstance(arg, str) else arg)

                if action == "retrieve":
                    if len(normalized_args) >= 2:
                        col, year = normalized_args[0], normalized_args[1]
                        values = _legacy_retrieving(df, [col, year])
                        context[step_name] = values[0] if values else None
                        computed_values[step_name] = context[step_name]
                    else:
                        context[step_name] = None

                elif action in NAMED_OPERATIONS:
                    func = NAMED_OPERATIONS[action]
                    resolved_vals = []
                    for arg in args:
                        val = _resolve_arg(arg, computed_values)
                        if val is None:
                            context[step_name] = None
                            break
                        resolved_vals.append(val)
                    else:
                        try:
                            context[step_name] = func(*resolved_vals)
                            computed_values[step_name] = context[step_name]
                        except Exception as e:
                            print(f"⚠️ Error in {action}: {e}")
                            context[step_name] = None
                else:
                    context[step_name] = None
                continue

            context[step_name] = None

        print("✅ Final context:", context)
        return context

    except Exception as e:
        print(f"❌ Error in execution: {str(e)}")
        raise


def _legacy_retrieving(df: pd.DataFrame, items: list[str]) -> list[float | None]:
    print(f"🔍 Attempting to retrieve: {items}")
    if len(items) == 2:
        col, year = items[0].strip(), items[1].strip()
        print(
            f"🔍 Looking for: '{col}' in index ({col in df.index}), "
            f"'{year}' in columns ({year in df.columns})"
        )
        if col in df.index and year in df.columns:
            val = df.loc[col, year]
            print(f"✅ Found value: {val}")
            return [float(val)]
    print("❌ Value not found")
    return [None]


def _resolve_arg(arg: Any, computed_values: dict[str, Any]) -> float | None:
    if isinstance(arg, str) and arg.startswith("step"):
        return computed_values.get(arg.strip())
    if isinstance(arg, (int, float)):
        return float(arg)
    try:
        return float(arg)
    except (ValueError, TypeError):
        return None