"""
Deterministic plan executor.

After the agent calls ``write_plan`` with a validated ``QueryPlan``, this
module executes every step as plain Python function calls — NO model in
the loop.  This turns a 10+ model-call query into 2 calls:

    Call 1: model calls write_plan → plan_executor runs all steps → results
            returned to the model in the tool response
    Call 2: model sees all results and produces ExecutionResult

The executor handles:
  - retrieve steps  → calls retrieve_values directly
  - compute steps   → calls execute_python_code directly
  - named math ops  → applies the operation to prior step results

Results are stored in ``ctx.deps.computed_values`` so the pipeline and
agent can reference them when building the final ``ExecutionResult``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from models import QueryPlan, PlanStep


# ============================================================================
# Named math operations (applied to prior step results or literals)
# ============================================================================

import math

_NAMED_OPS: dict[str, Any] = {
    # Unary (1 arg)
    "sqrt": lambda x: math.sqrt(_to_float(x)),
    "abs": lambda x: abs(_to_float(x)),
    "negate": lambda x: -_to_float(x),
    "exp": lambda x: math.exp(_to_float(x)),
    "log": lambda x: math.log(_to_float(x)),
    # Binary (2 args)
    "subtract": lambda a, b: _to_float(a) - _to_float(b),
    "divide": lambda a, b: _to_float(a) / _to_float(b) if _to_float(b) != 0 else None,
    "return_percentage": lambda a, b: (_to_float(a) / _to_float(b) * 100) if _to_float(b) != 0 else None,
    "power": lambda a, b: _to_float(a) ** _to_float(b),
    "yoy_growth": lambda a, b: ((_to_float(a) - _to_float(b)) / _to_float(b) * 100) if _to_float(b) != 0 else None,
    "ratio": lambda a, b: (_to_float(a) / _to_float(b)) if _to_float(b) != 0 else None,
    "percentage_change": lambda a, b: ((_to_float(a) - _to_float(b)) / _to_float(b) * 100) if _to_float(b) != 0 else None,
    "difference": lambda a, b: _to_float(a) - _to_float(b),
    # Ternary (3 args)
    "cagr": lambda end, start, n: ((_to_float(end) / _to_float(start)) ** (1 / _to_float(n)) - 1) * 100 if _to_float(start) != 0 else None,
    # N-ary (2+ args)
    "add": lambda *vals: sum(_to_float(v) for v in vals),
    "multiply": lambda *vals: math.prod(_to_float(v) for v in vals),
    "max": lambda *vals: max(_to_float(v) for v in vals),
    "min": lambda *vals: min(_to_float(v) for v in vals),
    "average": lambda *vals: sum(_to_float(v) for v in vals) / len(vals),
    "median": lambda *vals: _median([_to_float(v) for v in vals]),
    "stdev": lambda *vals: _stdev([_to_float(v) for v in vals]),
}


def _to_float(val: Any) -> float:
    """Convert a step result (string, number, or JSON) to float."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        # Handle "Sheet1: 1500.0; Sheet2: 1300.0" — take first value
        if ":" in val and ";" in val:
            val = val.split(";")[0].split(":")[-1].strip()
        elif ":" in val:
            val = val.split(":")[-1].strip()
        try:
            return float(val)
        except ValueError:
            # Try parsing as JSON dict (multi-year retrieval)
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    # Take the first numeric value
                    for v in parsed.values():
                        if isinstance(v, (int, float)):
                            return float(v)
                        if isinstance(v, dict):
                            for sv in v.values():
                                if isinstance(sv, (int, float)):
                                    return float(sv)
            except (json.JSONDecodeError, TypeError):
                pass
            raise ValueError(f"Cannot convert to float: {val}")
    raise ValueError(f"Cannot convert to float: {type(val)}")


def _median(values: list[float]) -> float:
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    if n % 2 == 0:
        return (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
    return sorted_vals[n // 2]


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(variance)


# ============================================================================
# Step executor — runs a single step, returns (step_name, result)
# ============================================================================

async def _execute_step(
    step_name: str,
    step: PlanStep,
    step_results: dict[str, Any],
    ctx: Any,  # RunContext[PipelineDeps]
) -> tuple[str, Any]:
    """Execute a single plan step deterministically (no model call).

    Returns (step_name, result).  Raises on unrecoverable errors.
    """
    action = step.action
    args = step.args or []

    if action == "retrieve":
        # args = ["FieldName", "Year1", "Year2", ...] or
        #        ["SheetName", "FieldName", "Year1", ...]
        from tools import retrieve_values
        sheet_names = _sheet_names_in_context(ctx)
        years_set = _years_in_context(ctx)
        if len(args) >= 3 and args[0] in sheet_names:
            # First arg is a sheet name
            sheet, field, years = args[0], args[1], args[2:]
        else:
            sheet, field, years = "", args[0], args[1:]
        result = retrieve_values(ctx, field, years, sheet=sheet)
        return step_name, result

    if action == "compute":
        # The agent should have provided a description, not code.
        # We can't run arbitrary code from a description, so we return
        # a message telling the model to use execute_python_code.
        # However, if the agent provided actual Python code in the
        # description, we try to run it.
        code = step.description or ""
        if "return" in code and ("=" in code or "return" in code):
            from tools import execute_python_code
            result = await execute_python_code(ctx, code)
            return step_name, result
        # No code — signal that the model needs to compute this
        return step_name, f"COMPUTE_NEEDED: {code}"

    if action in _NAMED_OPS:
        # Named math op — resolve args to prior step results or literals
        resolved = []
        for arg in args:
            if arg in step_results:
                resolved.append(step_results[arg])
            else:
                resolved.append(arg)  # literal number
        op_fn = _NAMED_OPS[action]
        try:
            result = op_fn(*resolved)
            return step_name, result
        except Exception as exc:
            return step_name, f"ERROR: {type(exc).__name__}: {exc}"

    return step_name, f"ERROR: unknown action '{action}'"


def _years_in_context(ctx: Any) -> set[str]:
    """Get available years from deps for sheet-name detection."""
    try:
        return {str(y) for y in ctx.deps.available_years}
    except Exception:
        return set()


def _sheet_names_in_context(ctx: Any) -> set[str]:
    """Get available sheet names from deps for sheet-name detection."""
    try:
        return {meta.sheet_name for meta in ctx.deps.sheet_metas}
    except Exception:
        return set()


# ============================================================================
# Plan executor — runs all steps in order, returns results dict
# ============================================================================

async def execute_plan(
    plan: QueryPlan,
    ctx: Any,  # RunContext[PipelineDeps]
) -> dict[str, Any]:
    """Execute a validated QueryPlan deterministically.

    Runs each step in order, passing prior results to subsequent steps.
    Stores all results in ctx.deps.computed_values and returns them.

    For compute steps where the agent provided a description (not code),
    the result is marked "COMPUTE_NEEDED" — the model will need to call
    execute_python_code for those.  This is the fallback for complex
    calculations that can't be expressed as named ops.
    """
    step_results: dict[str, Any] = {}

    # Handle retrieve_numbers (items list)
    if plan.task_type == "retrieve_numbers" and plan.items:
        from tools import retrieve_values
        for i, item in enumerate(plan.items, 1):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) >= 2:
                field, years = parts[0], parts[1:]
                step_name = f"step{i}"
                result = retrieve_values(ctx, field, years, sheet="")
                step_results[step_name] = result
                # retrieve_values already emits tool_call/tool_result events
        ctx.deps.computed_values.update(step_results)
        return step_results

    # Handle perform_calculations (plan dict)
    if plan.plan:
        for step_name, step in plan.plan.items():
            result_name, result = await _execute_step(step_name, step, step_results, ctx)
            step_results[result_name] = result
            ctx.deps.computed_values[result_name] = result
            # retrieve_values and execute_python_code emit their own events;
            # only emit for named ops (which don't emit internally)
            if step.action not in ("retrieve", "compute"):
                ctx.deps.emit("tool_result", {"tool": step.action, "step": result_name, "result": str(result)[:500]})

    return step_results


def format_results_for_model(step_results: dict[str, Any]) -> str:
    """Format step results as a string for the model to read in the tool response."""
    if not step_results:
        return "No steps executed."
    lines = ["PLAN EXECUTED. Here are ALL the results (do NOT call any more tools):"]
    for step_name, result in step_results.items():
        lines.append(f"  {step_name} = {result}")
    compute_needed = [k for k, v in step_results.items() if isinstance(v, str) and v.startswith("COMPUTE_NEEDED")]
    if compute_needed:
        lines.append("")
        lines.append("Steps marked COMPUTE_NEEDED require you to call execute_python_code")
        lines.append("with the actual calculation code. Use the retrieved values above.")
    else:
        lines.append("")
        lines.append("ALL STEPS COMPLETED. The data above is ALL you need.")
        lines.append("DO NOT call retrieve_values or execute_python_code.")
        lines.append("Produce your ExecutionResult NOW using these results.")
    return "\n".join(lines)
