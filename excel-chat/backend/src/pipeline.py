"""Pipeline orchestration for Excel-chat.

Active code: timing, fallback/retry, deterministic short-circuit, friendly
response builders, and build_query_pipeline (the public entry point).

The OLD multi-agent pipeline (planner → executor → responder) is preserved
as commented-out code at the bottom for review.

Shared models live in models.py. The agent lives in agent.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import contextmanager
from typing import Any, Callable

import pandas as pd

from sheet_metadata import SheetMeta
from guardrails import inject_disclaimer
from tools import PipelineDeps, retrieve_values
from observability import observe_agent_run, observe_step, init_observability
from models import (
    PlanStep,
    QueryPlan,
    ExecutionResult,
    _flatten_value,
    PRIMARY_MODEL,
    FALLBACK_MODEL,
)
from agent import (
    build_query_agent,
    validate_plan_semantics,
    validate_execution_result,
)

# Initialise Langfuse / Pydantic AI instrumentation before any Agent is built.
init_observability()


# ============================================================================
# Timing Instrumentation
# ============================================================================

@contextmanager
def timed(stage: str, timings: dict[str, float]):
    """Record elapsed wall-clock time for ``stage`` into ``timings``."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        timings[stage] = timings.get(stage, 0.0) + elapsed
        print(f"[{stage}] {elapsed:.2f}s")


# ============================================================================
# Fallback / Retry Infrastructure
# ============================================================================

def _is_empty_response_error(exc: Exception) -> bool:
    """Check whether *exc* is an empty-model-response error from pydantic-ai."""
    msg = str(exc).lower()
    return "empty model response" in msg or "received empty" in msg


def _is_fallback_worthy_error(exc: Exception) -> bool:
    """Check whether *exc* warrants a fallback to the secondary model."""
    msg = str(exc).lower()
    if _is_empty_response_error(exc):
        return True
    if any(kw in msg for kw in (
        "connection", "timeout", "timed out",
        "502", "503", "504", "500",
        "rate limit", "rate_limit", "too many requests",
        "service unavailable", "bad gateway", "internal server error",
    )):
        return True
    if "maximum retries" in msg and "validation" in msg:
        return True
    return False


class PipelineError(Exception):
    """Raised when all model attempts (primary + fallback + retry) fail."""
    pass


async def _run_with_fallback(
    build_agent: Callable[..., Any],
    prompt: str,
    *args,
    deps: Any = None,
    **kwargs,
) -> Any:
    """Run an agent with a 3-attempt retry strategy on recoverable errors.

    Attempt 1: primary model (PRIMARY_MODEL)
    Attempt 2: fallback model (FALLBACK_MODEL)
    Attempt 3: primary model again (after a short delay)

    If all 3 attempts fail with fallback-worthy errors, raises PipelineError.
    """
    attempts = [
        ("primary", None),
        ("fallback", FALLBACK_MODEL),
        ("primary-retry", None),
    ]

    last_exc: Exception | None = None
    message_history = kwargs.pop("message_history", None)

    for i, (label, model_name) in enumerate(attempts):
        if i > 0:
            await asyncio.sleep(2 * i)
        model_label = model_name or PRIMARY_MODEL
        try:
            kw = dict(kwargs)
            if model_name is not None:
                kw["model_name"] = model_name
            agent = build_agent(*args, **kw)
            with observe_step(
                name="excel-chat:agent_attempt",
                input={"stage": label, "model": model_label, "prompt": prompt[:500]},
                metadata={"stage": label, "model": model_label},
            ) as attempt_span:
                try:
                    run_kw: dict[str, Any] = {}
                    if deps is not None:
                        run_kw["deps"] = deps
                    if message_history is not None:
                        run_kw["message_history"] = message_history
                    # Allow more round-trips for complex multi-field queries
                    # (growth rate comparisons need retrieve + compute + synthesize)
                    from pydantic_ai.usage import UsageLimits
                    run_kw["usage_limits"] = UsageLimits(request_limit=100)
                    result = await agent.run(prompt, **run_kw)
                    if attempt_span:
                        attempt_span.set_output(
                            result.output if hasattr(result, "output") else result
                        )
                        attempt_span.record_usage(result.usage, 0)
                    return result
                except Exception as exc:
                    if attempt_span:
                        attempt_span.record(
                            decision="retry",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    raise
        except Exception as exc:
            if not _is_fallback_worthy_error(exc):
                raise
            last_exc = exc
            print(f"⚠️ Attempt {i+1} ({label}, {model_label}) failed: {type(exc).__name__}: {exc}")
            if i < len(attempts) - 1:
                next_label = attempts[i + 1][0]
                next_model = attempts[i + 1][1] or PRIMARY_MODEL
                print(f"   Retrying with {next_label} ({next_model})…")
            else:
                print(f"   All {len(attempts)} attempts exhausted.")

    raise PipelineError(
        "The AI model could not process this query after multiple attempts. "
        "Please try rephrasing your question or try again later."
    ) from last_exc


# ============================================================================
# Deterministic Response Builders (no LLM — for obvious lookups only)
# ============================================================================

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

    # Case 1: retrieve_numbers with items
    if plan.task_type == "retrieve_numbers" and plan.items:
        parts = []
        for i, item in enumerate(plan.items):
            fields = [f.strip() for f in item.split(",")]
            step_name = f"step{i+1}"
            if len(fields) == 2:
                field_name, year = fields
                val = step_results.get(step_name, final)
                parts.append(f"{field_name} in {year}: {val}")
            elif len(fields) == 3:
                sheet_name, field_name, year = fields
                val = step_results.get(step_name, final)
                parts.append(f"{field_name} in {year} ({sheet_name}): {val}")
        if parts:
            return "\n".join(parts)

        # Multi-item same field: format as a list of year: value pairs
        if len(plan.items) > 2:
            all_values: dict[str, Any] = {}
            for i, item in enumerate(plan.items):
                step_name = f"step{i+1}"
                raw = step_results.get(step_name, "")
                if isinstance(raw, str) and not raw.startswith("ERROR"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            all_values.update(parsed)
                    except (json.JSONDecodeError, TypeError):
                        pass
            if all_values:
                field_name = plan.items[0].split(",")[0].strip()
                lines = [f"Here are the {field_name} values for each year:"]
                for year in sorted(all_values.keys()):
                    lines.append(f"- **{year}**: {all_values[year]}")
                return "\n".join(lines)

    # Case 2: single-step plan with a clear explanation
    if plan.plan and len(plan.plan) == 1 and explanation:
        step_name = list(plan.plan.keys())[0]
        step = plan.plan[step_name]
        val = step_results.get(step_name, final)
        if step.action == "retrieve":
            return f"{explanation}: {val}"
        if val is not None and explanation:
            return f"{explanation}: {val}"

    return None


def _format_calculation_response(query: str, plan: QueryPlan | None, execution: ExecutionResult) -> str | None:
    """Deterministic fallback for calculation results when the model leaves
    friendly_response empty. Builds a readable answer from step_results and
    the plan's step descriptions, referencing actual field names.

    Returns None if there's not enough information to build a meaningful response.
    """
    step_results = execution.step_results or {}
    final = execution.final_answer
    if final is None:
        return None

    # No plan (advice/EDA) — just state the final answer
    if plan is None or not plan.plan:
        return f"The answer is: {final}"

    steps = plan.plan
    # Collect retrieve step descriptions for context
    retrieve_descs: list[str] = []
    for sid, step in steps.items():
        if step.action == "retrieve" and step.description:
            retrieve_descs.append(step.description)
        elif step.action == "retrieve" and step.args:
            field = step.args[0] if not _looks_like_year(step.args[1] if len(step.args) > 1 else "") else ""
            if not field and len(step.args) > 1:
                field = step.args[1]
            years = [a for a in step.args if _looks_like_year(a)]
            if field and years:
                retrieve_descs.append(f"{field} ({', '.join(years)})")

    # Find the terminal step (last non-retrieve, or last step)
    terminal_sid: str | None = None
    for sid, step in reversed(list(steps.items())):
        if step.action != "retrieve":
            terminal_sid = sid
            break
    if terminal_sid is None and steps:
        terminal_sid = list(steps.keys())[-1]

    terminal_step = steps.get(terminal_sid) if terminal_sid else None
    terminal_desc = terminal_step.description if terminal_step else "the result"

    # Build a readable response
    parts: list[str] = []
    if retrieve_descs:
        parts.append(f"Based on: {'; '.join(retrieve_descs)}.")

    # If final_answer is raw JSON (agent forgot to synthesize), try to
    # extract a human-readable summary from it instead of dumping the dict.
    final_str = str(final)
    if final_str.strip().startswith("{") or final_str.strip().startswith("["):
        try:
            parsed = json.loads(final_str)
            if isinstance(parsed, dict):
                # Look for a "conclusion" or "result" key
                for key in ("conclusion", "result", "answer", "summary"):
                    if key in parsed:
                        parts.append(str(parsed[key]))
                        # Add supporting numbers
                        for k, v in parsed.items():
                            if k != key and isinstance(v, (int, float)):
                                parts.append(f"({k}: {v})")
                        return " ".join(parts)
                # No conclusion key — format key-value pairs
                kv = [f"{k}: {v}" for k, v in parsed.items()
                      if isinstance(v, (int, float, str)) and not str(v).startswith("{")]
                if kv:
                    parts.append(", ".join(kv))
                    return " ".join(parts)
        except (json.JSONDecodeError, TypeError):
            pass

    parts.append(f"{terminal_desc.capitalize() if terminal_desc else 'Result'}: {final}")
    return " ".join(parts)


# ============================================================================
# Deterministic Short-Circuit (0 LLM calls for unambiguous single-field lookups)
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


_CALC_KEYWORDS = (
    "growth", "grow", "rate", "ratio", "percent", "%", "average", "avg",
    "compare", "comparison", "difference", "cagr", "trend", "increase",
    "decrease", "change", "stability", "stable", "highest", "lowest",
    "maximum", "minimum", "max", "min", "median", "stdev", "fraction",
    "sum of", "total of", "between", "vs", "versus", "by how much",
    "how much did", "analyze", "stand out", "allocat", "capital as",
)


def _try_deterministic_lookup(
    query: str, sheet_metas: list[SheetMeta]
) -> tuple[str, str] | None:
    """Match unambiguous single-field lookup queries for the 0-LLM path.

    Returns (field, year) if the query is a simple lookup that can be answered
    deterministically, else None. Conservative by design.
    """
    import re

    q = query.strip().rstrip("?").strip()
    q_lower = q.lower()

    years = re.findall(r"\b(?:19|20)\d{2}\b", q)
    if len(years) != 1:
        return None

    if any(kw in q_lower for kw in _CALC_KEYWORDS):
        return None

    if not re.match(r"^(what\s+(was|is|were|are)\b|how\s+much\s+(was|is)\b|value\s+of\b)", q_lower):
        return None

    matched_field: str | None = None
    for meta in sheet_metas:
        for f in meta.fields:
            if len(f) >= 4 and f.lower() in q_lower:
                if matched_field is not None and matched_field.lower() != f.lower():
                    return None
                matched_field = f
    if matched_field is None:
        return None
    return (matched_field, years[0])


# ============================================================================
# Public Pipeline API
# ============================================================================

def build_query_pipeline(
    sheets: dict[str, pd.DataFrame],
    sheet_metas: list[SheetMeta],
    user_id: str = "anonymous",
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> Any:
    """Build the query pipeline.

    Returns a callable that accepts a query string and returns the pipeline
    result dict. The agent creates its own OpenRouter model internally.
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
        """Run the single agent: plan (validated) -> execute (tools) -> answer."""
        timings: dict[str, float] = {}
        stage_usages: list[Any] = []

        with observe_agent_run(
            name=f"excel-chat:query: {query[:80]}",
            user_id=user_id,
            tags=["excel-chat", "query"],
            metadata={"query": query[:500]},
        ) as trace_span:
            deps = PipelineDeps(
                sheets=sheets,
                sheet_metas=sheet_metas,
                original_query=query,
                available_fields=all_fields,
                available_years=[str(y) for y in all_years],
                user_id=user_id,
                on_event=_emit,
            )

            # ---- Deterministic short-circuit (0 LLM calls) ----
            deterministic = _try_deterministic_lookup(query, sheet_metas)
            if deterministic is not None:
                field, year = deterministic
                _emit("status", {"message": "Retrieving data from sheets…"})
                with timed("short_circuit", timings):
                    with observe_step(
                        name="excel-chat:decision:short_circuit",
                        input={"path": "deterministic", "field": field, "year": year},
                    ) as decision_span:
                        from types import SimpleNamespace
                        ctx = SimpleNamespace(deps=deps)
                        raw = retrieve_values(ctx, field, [year], sheet="")
                        step_results = {"step1": raw}
                        if decision_span:
                            decision_span.record(decision="skip_agent", output=step_results)
                _emit("tool_result", {"tool": "retrieve_values", "result": str(raw)[:500]})
                _emit("status", {"message": "All values retrieved — preparing answer…"})

                if str(raw).startswith("ERROR"):
                    final_answer: Any = raw
                else:
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            final_answer = _flatten_value(parsed)
                        else:
                            final_answer = parsed
                    except (json.JSONDecodeError, TypeError):
                        final_answer = raw
                execution = ExecutionResult(
                    step_results=step_results,
                    final_answer=final_answer,
                    explanation="Retrieved directly from the DataFrame (no LLM call required).",
                    friendly_response="",
                )
                friendly = inject_disclaimer(
                    f"**{field}** in {year}: {_flatten_value(final_answer)}"
                )
                print(f"⚡ Skipped agent (deterministic lookup) — {field} {year}")
                if friendly:
                    print(f"Response: {friendly[:100]}...")
                _emit("friendly", {"response": friendly})
                total = sum(timings.values())
                print(f"Pipeline total: {total:.2f}s | {timings}")
                if trace_span:
                    trace_span.record_totals(tokens_in=0, tokens_out=0, cost_usd=None, time_ms=total * 1000)
                return {
                    "answer": execution.model_dump(),
                    "plan": None,
                    "task_type": "retrieve_numbers",
                    "friendly_response": friendly,
                    "timings": timings,
                }

            # ---- Agent run: plan (write_plan) -> tools -> ExecutionResult ----
            _emit("status", {"message": "Analyzing your question…"})
            with observe_step(
                name="excel-chat:decision:short_circuit",
                input={"path": "agent", "query": query[:200]},
            ) as decision_span:
                if decision_span:
                    decision_span.record(decision="run_agent")
                with timed("agent_run", timings):
                    with observe_step(
                        name="excel-chat:agent_run",
                        input={"query": query[:500]},
                    ) as agent_span:
                        agent_result = await _run_with_fallback(
                            build_query_agent, query, sheet_metas,
                            deps=deps, query=query
                        )
                        if agent_span:
                            agent_span.record_usage(
                                agent_result.usage, timings.get("agent_run", 0) * 1000
                            )
                            stage_usages.append(agent_result.usage)

                        # ---- Output validation with retry-with-feedback ----
                        execution: ExecutionResult = agent_result.output
                        plan = deps.plan
                        message_history = agent_result.all_messages()

                        for retry_round in range(2):
                            out_errors = validate_execution_result(
                                execution, plan, sheet_metas
                            )
                            if not out_errors:
                                break
                            print(
                                f"⚠️ Output validation failed (round {retry_round+1}/2): "
                                + "; ".join(out_errors)
                            )
                            with observe_step(
                                name="excel-chat:decision:output_validation",
                                input={"round": retry_round + 1, "errors": out_errors},
                            ) as val_span:
                                if val_span:
                                    val_span.record(
                                        decision="retry",
                                        error="; ".join(out_errors),
                                    )
                            feedback = (
                                "OUTPUT REJECTED — fix these and return a corrected "
                                "ExecutionResult:\n"
                                + "\n".join(f"- {e}" for e in out_errors)
                            )
                            retry_result = await _run_with_fallback(
                                build_query_agent,
                                feedback,
                                sheet_metas,
                                deps=deps,
                                query=query,
                                message_history=message_history,
                            )
                            if agent_span:
                                agent_span.record_usage(
                                    retry_result.usage,
                                    timings.get("agent_run", 0) * 1000,
                                )
                                stage_usages.append(retry_result.usage)
                            execution = retry_result.output
                            message_history = retry_result.all_messages()

                        final_out_errors = validate_execution_result(
                            execution, plan, sheet_metas
                        )
                        if final_out_errors:
                            print(
                                "⚠️ Output still invalid after retries (proceeding "
                                "best-effort): " + "; ".join(final_out_errors)
                            )
                        if agent_span:
                            agent_span.set_output(execution.model_dump())

            task_type = plan.task_type if plan else "other"
            print("✅ Execution:", json.dumps(execution.model_dump(), indent=2, default=str))
            _emit("execution", {
                "step_results": execution.step_results,
                "final_answer": execution.final_answer,
                "explanation": execution.explanation,
            })

            # Post-execution structured key derivation (result cache layer 3)
            try:
                from result_cache import cache_step_results
                cache_step_results(user_id, plan, execution)
            except Exception as e:
                print(f"⚠️ Post-execution caching failed: {e}")

            friendly = inject_disclaimer(
                execution.friendly_response
                or _format_simple_response(query, plan, execution)
                or _format_calculation_response(query, plan, execution)
                or ""
            )
            if friendly:
                print(f"Response: {friendly[:100]}...")
            else:
                print("⚠️ No friendly response generated")
            _emit("friendly", {"response": friendly})

            total = sum(timings.values())
            print(f"Pipeline total: {total:.2f}s | {timings}")
            if trace_span:
                _tokens_in = sum(getattr(u, "input_tokens", 0) or 0 for u in stage_usages)
                _tokens_out = sum(getattr(u, "output_tokens", 0) or 0 for u in stage_usages)
                _cost = None
                _costs = [
                    float(c) for u in stage_usages
                    if (c := getattr(u, "cost", None)) is not None
                ]
                if _costs:
                    _cost = sum(_costs)
                trace_span.record_totals(
                    tokens_in=_tokens_in,
                    tokens_out=_tokens_out,
                    cost_usd=_cost,
                    time_ms=total * 1000,
                )
            return {
                "answer": execution.model_dump(),
                "plan": plan.model_dump() if plan else None,
                "task_type": task_type,
                "friendly_response": friendly,
                "timings": timings,
            }

    return run_pipeline


# ============================================================================
# OLD PLANNER / EXECUTOR / RESPONDER BUILDERS (retired — see agent.py)
# ============================================================================

# ============================================================================
# OLD PLANNER / EXECUTOR / RESPONDER BUILDERS (retired — see agent.py)
# ============================================================================

#     """Agent that generates a structured QueryPlan from a user question."""
#     model = build_openrouter_model(model_name)

#     # Build multi-sheet context
#     sheet_context_parts = []
#     all_fields = set()
#     all_years = set()
#     for meta in sheet_metas:
#         all_fields.update(meta.fields)
#         all_years.update(meta.years)
#         group_note = f" (schema group: {meta.schema_group})" if meta.schema_group and meta.schema_group != "unique" else ""
#         desc_note = f"\n      Description: {meta.combined_description}" if meta.combined_description != "No description available." else ""
#         sheet_context_parts.append(
#             f"  - Sheet '{meta.sheet_name}' (file: {meta.file_name}){group_note}\n"
#             f"      Fields: {', '.join(meta.fields[:20])}\n"
#             f"      Years: {', '.join(meta.years)}{desc_note}"
#         )
#     sheet_context = "\n".join(sheet_context_parts)
#     fields_str = ", ".join(sorted(all_fields))
#     years_str = ", ".join(sorted(all_years))

#     # Identify schema groups for cross-sheet note
#     groups = {}
#     for meta in sheet_metas:
#         if meta.schema_group and meta.schema_group != "unique":
#             groups.setdefault(meta.schema_group, []).append(meta.sheet_name)
#     cross_sheet_note = ""
#     if groups:
#         group_descs = [f"  - {', '.join(sheets_list)}" for sheets_list in groups.values()]
#         cross_sheet_note = f"\n    Cross-sheet calculations are possible for sheets with the same schema group:\n{chr(10).join(group_descs)}\n    When retrieving from a schema group, omit the sheet name to search all sheets in that group."

#     instructions = f"""You are a financial data analysis planner.

#     Available sheets and their data:
# {sheet_context}

#     All available fields across sheets: {fields_str}
#     All available years across sheets: {years_str}
# {cross_sheet_note}

#     Task types:
#     - retrieve_numbers: Simple lookups (e.g., "What was revenue in 2022?")
#     - perform_calculations: Any math or comparison (e.g., "What's the profit margin?", "Compare YoY growth")
#     - give_advice: Recommendations or analysis
#     - other: Everything else

#     For retrieve_numbers: return items like ["Revenue, 2022"] or ["SheetName, Revenue, 2022"] for sheet-specific retrieval.
#     For perform_calculations: return a plan using these step types:

#     1. retrieve — fetch value(s) from the DataFrame. Pass one or more years.
#        For a single year: args = ["FieldName", "Year"] (searches all sheets) or ["SheetName", "FieldName", "Year"] (specific sheet)
#        For multiple years: args = ["FieldName", "Year1", "Year2", ...] (searches all sheets) or ["SheetName", "FieldName", "Year1", "Year2", ...] (specific sheet)
#        Example: step1: retrieve ["Capital expenditure", "2018", "2019", "2020", "2021", "2022"]
#     2. Named math operations — apply to prior step results or literal numbers:
#        - Unary (1 arg): sqrt, abs, negate, exp
#        - Binary (2 args): subtract, divide, return_percentage, power, log, yoy_growth, ratio, percentage_change, difference
#        - Ternary (3 args): cagr [end_value, start_value, num_years]
#        - N-ary (2+ args): add, multiply, max, min, average, median, stdev
#        Args can be step references (e.g. "step1") or literal numbers (e.g. "100").
#     3. compute — for complex multi-step calculations that can't be expressed with named ops,
#        provide a natural-language description of the calculation as args[0].
#        The executor will translate this into Python code and run it in a sandbox.

#     Prefer named operations for simple math. Use compute for complex formulas,
#     multi-step calculations, or when you need math module functions not covered above.

#     When a query involves data from multiple sheets (especially in the same schema group),
#     use retrieve with specific sheet names to get values from each sheet, then apply
#     named operations or compute to combine them.

#     Examples:

#     [Simple percentage - single sheet]
#     step1: retrieve ["Wages and salaries", "2022"]
#     step2: retrieve ["Expense", "2022"]
#     step3: return_percentage ["step1", "step2"]

#     [YoY growth]
#     step1: retrieve ["Revenue", "2023"]
#     step2: retrieve ["Revenue", "2022"]
#     step3: yoy_growth ["step1", "step2"]

#     [Cross-sheet comparison - same schema group]
#     step1: retrieve ["Sheet1", "Revenue", "2022"]
#     step2: retrieve ["Sheet2", "Revenue", "2022"]
#     step3: subtract ["step1", "step2"]

#     [Cross-sheet average across all sheets]
#     step1: retrieve ["Revenue", "2022"]  (returns values from all matching sheets)
#     step2: compute ["Calculate the average of all Revenue 2022 values retrieved in step1"]

#     [CAGR over 5 years]
#     step1: retrieve ["Revenue", "2023"]
#     step2: retrieve ["Revenue", "2018"]
#     step3: cagr ["step1", "step2", "5"]

#     [Multi-year batch]
#     step1: retrieve ["Capital expenditure", "2018", "2019", "2020", "2021", "2022"]
#     step2: compute ["Find the year with the lowest capital expenditure among step1"]

#     [Complex calculation via compute]
#     step1: retrieve ["Revenue", "2023"]
#     step2: retrieve ["Revenue", "2021"]
#     step3: retrieve ["Expenses", "2023"]
#     step4: compute ["Calculate the compound annual growth rate of Revenue from 2021 to 2023, then multiply by the 2023 profit margin (Revenue - Expenses) / Revenue"]

#     Always use exact field names and years from the available lists.
#     {GUARDRAIL_SYSTEM_PROMPT}
#     """

#     return Agent(
#         model,
#         output_type=QueryPlan,
#         system_prompt=instructions,
#         model_settings={"temperature": 0.1},
#         retries=2,
#         capabilities=[Instrumentation()],
#     )


# def build_executor_agent(sheets: dict[str, pd.DataFrame], sheet_metas: list[SheetMeta], model_name: str | None = None) -> Agent[PipelineDeps, ExecutionResult]:
#     """Agent that executes a plan using tools, with structured output."""
#     model = build_openrouter_model(model_name)

#     retrieve_t = Tool(retrieve_values, prepare=_prepare_retrieve_values_tool)

#     sheet_names = list(sheets.keys())
#     system_prompt = f"""You are a financial data execution engine.

#     You have access to {len(sheets)} sheet(s): {', '.join(sheet_names)}

#     ## Tools:
#     1. retrieve_values(field, years: list[str], sheet="") — fetch value(s) for a field.
#        Single year ["2023"] → string like "1500.0". Multiple years ["2020","2021"] → JSON dict like {{"2020": 1500.0}}.
#        No sheet name → searches all sheets.
#     2. execute_python_code — run Python in a sandbox for ANY calculation.
#        Supports: math module, builtins (abs, round, min, max, sum, len, sorted),
#        NumPy (np_mean, np_std, np_median, np_sum, np_percentile, np_diff, np_sqrt, etc.),
#        Pandas (pd_describe, pd_value_counts, pd_rolling_mean, np_histogram).
#     3. EDA tools: analyze_sheet, find_missing_data, discover_column_types, get_sheet_shape,
#        group_summary_stats, create_pivot_table, crosstab_analysis, drop_missing_columns,
#        convert_to_numerical, correlation_analysis, get_sheet_head, get_sheet_info,
#        value_counts_analysis, deduplicate_rows, get_sheet_dtypes.
#        Use these for data quality, distributions, correlations, or summary questions.

#     ## Execution flow:
#     1. Call retrieve_values for all needed values (pass multiple years in one call per field).
#     2. For named ops (add, subtract, multiply, divide, return_percentage, sqrt, power, log, exp,
#        abs, negate, max, min, average, median, stdev, yoy_growth, cagr, ratio, percentage_change,
#        difference): compute directly in Python or use execute_python_code.
#     3. For compute steps: call execute_python_code with retrieved values as literal numbers.
#        Use a `return` statement. Example: 'rev = 1500\\nexp = 800\\nreturn (rev - exp) / rev * 100'
#     4. For EDA questions, use EDA tools directly.
#     5. Return the final answer as structured data.

#     ### CRITICAL — step_results MUST contain EVERY step's result (never empty dict).
#     Example: plan has step1 (retrieve), step2 (retrieve), step3 (compute) →
#     step_results = {{"step1": {{...}}, "step2": {{...}}, "step3": <computed_value>}}.
#     ### CRITICAL — friendly_response MUST use actual field names from the plan, not generic phrases.
#     WRONG: "The first series grew at 25.4%". RIGHT: "Social security benefits grew at a CAGR of 25.4%".
#     When retrieve returns multiple sheet values, format is "SheetName: value; SheetName2: value2".
#     If a retrieval fails, note it and continue.
#     {GUARDRAIL_SYSTEM_PROMPT}
#     """

#     return Agent(
#         model,
#         deps_type=PipelineDeps,
#         output_type=ExecutionResult,
#         system_prompt=system_prompt,
#         tools=[
#             retrieve_t,
#             execute_python_code,
#             analyze_sheet,
#             find_missing_data,
#             discover_column_types,
#             get_sheet_shape,
#             group_summary_stats,
#             create_pivot_table,
#             crosstab_analysis,
#             drop_missing_columns,
#             convert_to_numerical,
#             correlation_analysis,
#             get_sheet_head,
#             get_sheet_info,
#             value_counts_analysis,
#             deduplicate_rows,
#             get_sheet_dtypes,
#         ],
#         model_settings={"temperature": 0.1},
#         retries=2,
#         capabilities=[Instrumentation()],
#     )


# def build_responder_agent(model_name: str | None = None) -> Agent[None, str]:
#     """Agent that generates a friendly natural language response."""
#     model = build_openrouter_model(model_name)

#     return Agent(
#         model,
#         output_type=str,
#         system_prompt=f"""You are a helpful financial assistant.

#         Given a user's question and the calculated results, provide a clear, conversational
#         response that directly answers the question. Include specific numerical values
#         with proper formatting (currency, percentages). Briefly explain how the answer
#         was derived. Use a friendly, professional tone.
#         {GUARDRAIL_SYSTEM_PROMPT}
#         """,
#         model_settings={"temperature": 0.3},
#         capabilities=[Instrumentation()],
#     )


# ============================================================================
# OLD run_pipeline body (retired — see agent.py build_query_pipeline)
# ============================================================================

#     async def run_pipeline(query: str) -> dict:
#         # Optimization 6: per-stage timing dict returned alongside the answer.
#         timings: dict[str, float] = {}
#         stage_usages: list[Any] = []

#         with observe_agent_run(
#             name="excel-chat:query",
#             user_id=user_id,
#             tags=["excel-chat", "query"],
#         ) as trace_span:
#             # Step 1: Plan
#             _emit("status", {"message": "Analyzing your question…"})
#             with timed("planner", timings):
#                 with observe_step(
#                     name="excel-chat:step:planner",
#                     input={"query": query[:500]},
#                 ) as planner_span:
#                     plan_result = await _run_with_fallback(
#                         build_planner_agent, query, sheets, sheet_metas
#                     )
#                     if planner_span:
#                         planner_span.record_usage(
#                             plan_result.usage, timings.get("planner", 0) * 1000
#                         )
#                         stage_usages.append(plan_result.usage)
#             plan: QueryPlan = plan_result.output
#             print("Plan:", json.dumps(plan.model_dump(), indent=2))

#             # Emit classified intent + plan
#             _emit("plan", {
#                 "task_type": plan.task_type,
#                 "plan": plan.model_dump().get("plan"),
#                 "items": plan.items,
#                 "description": plan.description,
#             })

#             # Step 2: Execute
#             deps = PipelineDeps(
#                 sheets=sheets,
#                 sheet_metas=sheet_metas,
#                 original_query=query,
#                 available_fields=all_fields,
#                 available_years=all_years,
#                 user_id=user_id,
#             )

#             # ------------------------------------------------------------------
#             # Optimization 2: pre-populate retrieve steps in pure
#             # Python. The executor then only has to handle compute / friendly
#             # response, which collapses the LLM round-trip count.
#             # ------------------------------------------------------------------
#             pre_populated: dict[str, Any] = {}
#             if plan.plan:
#                 _emit("status", {"message": "Retrieving data from sheets…"})
#                 with timed("pre_populate", timings):
#                     with observe_step(
#                         name="excel-chat:step:pre_populate"
#                     ) as prepop_span:
#                         pre_populated = _prepopulate_retrievals(plan, deps)
#                         if prepop_span:
#                             prepop_span.record_usage(
#                                 None, timings.get("pre_populate", 0) * 1000
#                             )
#                 if pre_populated:
#                     print(f"Pre-populated {len(pre_populated)} values: {pre_populated}")
#                     _emit("pre_populated", {"values": pre_populated})

#             # ------------------------------------------------------------------
#             # Short-circuit: if every step is a retrieve, we don't
#             # need the executor at all. Build the ExecutionResult directly.
#             # ------------------------------------------------------------------
#             if plan.plan and _plan_is_pure_retrieve(plan):
#                 with observe_step(
#                     name="excel-chat:decision:short_circuit",
#                     input={"path": "pure_retrieve"},
#                 ) as decision_span:
#                     step_results: dict[str, Any] = {}
#                     final_answers: list[Any] = []
#                     for name, step in plan.plan.items():
#                         val = pre_populated.get(name)
#                         step_results[name] = val
#                         if not str(val).startswith("ERROR"):
#                             final_answers.append(val)
#                     # Single value → scalar; multiple → list.
#                     final_answer: Any = (
#                         final_answers[0] if len(final_answers) == 1 else final_answers
#                     )
#                     execution = ExecutionResult(
#                         step_results=step_results,
#                         final_answer=final_answer,
#                         explanation=(
#                             f"Retrieved {len(final_answers)} value(s) directly from the "
#                             "DataFrame (no executor LLM call required)."
#                         ),
#                         friendly_response="",  # filled in by _format_simple_response
#                     )
#                     if decision_span:
#                         decision_span.record(
#                             decision="skip_executor", output=execution.step_results
#                         )
#                 _emit("status", {"message": "All values retrieved — preparing answer…"})
#                 print("Skipped executor — pure retrieve plan.")
#                 try:
#                     from result_cache import cache_step_results
#                     cache_step_results(user_id, plan, execution)
#                 except Exception as e:
#                     print(f"⚠️ Post-execution caching failed: {e}")
#                 friendly = inject_disclaimer(_format_simple_response(query, plan, execution) or "")
#                 if friendly:
#                     print(f"Response: {friendly[:100]}...")
#                 _emit("friendly", {"response": friendly})
#                 total = sum(timings.values())
#                 print(f"Pipeline total: {total:.2f}s | {timings}")
#                 if trace_span:
#                     _tokens_in = sum(
#                         getattr(u, "input_tokens", 0) or 0 for u in stage_usages
#                     )
#                     _tokens_out = sum(
#                         getattr(u, "output_tokens", 0) or 0 for u in stage_usages
#                     )
#                     _cost = None
#                     _costs = [
#                         float(c) for u in stage_usages
#                         if (c := getattr(u, "cost", None)) is not None
#                     ]
#                     if _costs:
#                         _cost = sum(_costs)
#                     trace_span.record_totals(
#                         tokens_in=_tokens_in,
#                         tokens_out=_tokens_out,
#                         cost_usd=_cost,
#                         time_ms=total * 1000,
#                     )
#                 return {
#                     "answer": execution.model_dump(),
#                     "plan": plan.model_dump(),
#                     "task_type": plan.task_type,
#                     "friendly_response": friendly,
#                     "timings": timings,
#                 }

#             # ------------------------------------------------------------------
#             # Short-circuit 2: retrieve_numbers with items but no plan steps.
#             # The planner returned a list of "Field, Year" strings instead of
#             # structured steps. Parse them and retrieve directly — no executor
#             # LLM call needed.
#             # ------------------------------------------------------------------
#             if plan.task_type == "retrieve_numbers" and plan.items and not plan.plan:
#                 with observe_step(
#                     name="excel-chat:decision:short_circuit",
#                     input={"path": "retrieve_numbers_items"},
#                 ) as decision_span:
#                     _emit("status", {"message": "Retrieving data from sheets…"})
#                     from types import SimpleNamespace
#                     ctx = SimpleNamespace(deps=deps)
#                     step_results: dict[str, Any] = {}
#                     final_answers: list[Any] = []

#                     # Parse all items to extract (field, years) pairs.
#                     # Planner items can be:
#                     #   "Field, Year"                    → 2 parts
#                     #   "Category, Subcategory, Year"    → 3 parts (e.g. "Grants, To foreign governments, 2015")
#                     #   "Sheet, Field, Year"             → 3 parts
#                     # The last part is always the year; the second-to-last is the field name.
#                     parsed_items: list[tuple[str, list[str]]] = []
#                     for item in plan.items:
#                         parts = [p.strip() for p in item.split(",")]
#                         if len(parts) < 2:
#                             parsed_items.append((item, []))
#                             continue
#                         year = parts[-1]
#                         field = parts[-2]
#                         # If there are 4+ parts, join middle parts as the field name
#                         if len(parts) > 3:
#                             field = ", ".join(parts[1:-1])
#                         parsed_items.append((field, [year]))

#                     # Optimization: if all items share the same field, retrieve once
#                     # with all unique years instead of N separate calls
#                     item_fields: set[str] = set()
#                     for f, _ in parsed_items:
#                         if f:
#                             item_fields.add(f)
#                     if len(item_fields) == 1 and len(parsed_items) > 1:
#                         field = parsed_items[0][0]
#                         item_years = sorted({y for _, yrs in parsed_items for y in yrs})
#                         try:
#                             raw = retrieve_values(ctx, field, item_years, sheet="")
#                             step_results["step1"] = raw
#                             if not str(raw).startswith("ERROR"):
#                                 try:
#                                     parsed = json.loads(raw)
#                                     final_answers.append(parsed)
#                                 except (json.JSONDecodeError, TypeError):
#                                     final_answers.append(raw)
#                         except Exception as e:
#                             step_results["step1"] = f"ERROR: {type(e).__name__}: {e}"
#                     else:
#                         # Different fields — retrieve each separately
#                         for i, (field, years) in enumerate(parsed_items):
#                             step_name = f"step{i+1}"
#                             if not years:
#                                 step_results[step_name] = f"ERROR: cannot parse item '{plan.items[i]}'"
#                                 continue
#                             try:
#                                 raw = retrieve_values(ctx, field, years, sheet="")
#                                 step_results[step_name] = raw
#                                 if not str(raw).startswith("ERROR"):
#                                     if len(years) > 1:
#                                         try:
#                                             parsed = json.loads(raw)
#                                             final_answers.append(parsed)
#                                         except (json.JSONDecodeError, TypeError):
#                                             final_answers.append(raw)
#                                     else:
#                                         final_answers.append(raw)
#                             except Exception as e:
#                                 step_results[step_name] = f"ERROR: {type(e).__name__}: {e}"

#                     final_answer: Any = (
#                         final_answers[0] if len(final_answers) == 1 else final_answers
#                     )
#                     execution = ExecutionResult(
#                         step_results=step_results,
#                         final_answer=final_answer,
#                         explanation=(
#                             f"Retrieved {len(final_answers)} value(s) directly from "
#                             "the DataFrame (no executor LLM call required)."
#                         ),
#                         friendly_response="",
#                     )
#                     if decision_span:
#                         decision_span.record(
#                             decision="skip_executor", output=step_results
#                         )
#                 _emit("pre_populated", {"values": step_results})
#                 _emit("status", {"message": "All values retrieved — preparing answer…"})
#                 print(f"Skipped executor — retrieve_numbers with {len(plan.items)} items, no plan steps.")

#                 # Format friendly response from the retrieved data
#                 friendly_text = ""
#                 if final_answers:
#                     # Deduplicated case: single dict with multiple years
#                     first = final_answers[0]
#                     if isinstance(first, dict) and len(final_answers) == 1:
#                         field_name = parsed_items[0][0] if parsed_items else "value"
#                         lines = [f"Here are the {field_name} values for each year:"]
#                         for year in sorted(first.keys()):
#                             lines.append(f"- **{year}**: {first[year]}")
#                         friendly_text = "\n".join(lines)
#                     else:
#                         # Multiple items — each is a single-year value or dict
#                         parts = []
#                         for i, ans in enumerate(final_answers):
#                             field_name = parsed_items[i][0] if i < len(parsed_items) else f"item{i+1}"
#                             year = parsed_items[i][1][0] if i < len(parsed_items) and parsed_items[i][1] else ""
#                             # Extract scalar from single-year dict
#                             if isinstance(ans, dict) and len(ans) == 1:
#                                 val = list(ans.values())[0]
#                             else:
#                                 val = ans
#                             parts.append(f"**{field_name}** in {year}: {val}")
#                         friendly_text = "\n".join(parts)

#                 friendly = inject_disclaimer(friendly_text)
#                 if friendly:
#                     print(f"Response: {friendly[:100]}...")
#                 _emit("friendly", {"response": friendly})
#                 total = sum(timings.values())
#                 print(f"Pipeline total: {total:.2f}s | {timings}")
#                 if trace_span:
#                     _tokens_in = sum(
#                         getattr(u, "input_tokens", 0) or 0 for u in stage_usages
#                     )
#                     _tokens_out = sum(
#                         getattr(u, "output_tokens", 0) or 0 for u in stage_usages
#                     )
#                     _cost = None
#                     _costs = [
#                         float(c) for u in stage_usages
#                         if (c := getattr(u, "cost", None)) is not None
#                     ]
#                     if _costs:
#                         _cost = sum(_costs)
#                     trace_span.record_totals(
#                         tokens_in=_tokens_in,
#                         tokens_out=_tokens_out,
#                         cost_usd=_cost,
#                         time_ms=total * 1000,
#                     )
#                 return {
#                     "answer": execution.model_dump(),
#                     "plan": plan.model_dump(),
#                     "task_type": plan.task_type,
#                     "friendly_response": friendly,
#                     "timings": timings,
#                 }

#             _emit("status", {"message": "Running calculations…"})

#             # Build execution prompt from the plan
#             if plan.task_type == "retrieve_numbers" and plan.items:
#                 exec_prompt = (
#                     f"Retrieve the following values and return them as the final answer: {plan.items}"
#                 )
#             elif plan.plan:
#                 retrieve_steps = []
#                 named_steps = []
#                 compute_desc = ""
#                 # Build a step-to-field-name mapping so the executor knows which
#                 # field each step refers to (for use in friendly_response).
#                 step_field_map: list[str] = []
#                 for name, step in plan.plan.items():
#                     if step.action == "retrieve":
#                         # Extract field name from args for context
#                         if _looks_like_year(step.args[1] if len(step.args) > 1 else ""):
#                             field_name = step.args[0] if step.args else ""
#                         else:
#                             field_name = step.args[1] if len(step.args) > 1 else ""
#                         step_field_map.append(f"  {name} → field: {field_name}")
#                         # If we already pre-populated this step, tell the executor
#                         # to use the literal value instead of re-fetching.
#                         if name in pre_populated:
#                             retrieve_steps.append(
#                                 f"  {name} (field: {field_name}): ALREADY DONE — value is {pre_populated[name]}"
#                             )
#                         else:
#                             retrieve_steps.append(f"  {name} (field: {field_name}): retrieve({step.args})")
#                     elif step.action == "compute":
#                         compute_desc = step.args[0] if step.args else ""
#                     elif step.action in NAMED_OPERATIONS:
#                         named_steps.append(f"  {name}: {step.action}({step.args})")
#                 steps_desc = "\n".join(retrieve_steps)
#                 named_desc = "\n".join(named_steps)
#                 pre_populated_block = _format_pre_populated_for_prompt(pre_populated)
#                 parts = []
#                 if pre_populated_block:
#                     parts.append(pre_populated_block)
#                 if retrieve_steps:
#                     parts.append(f"Retrieve these values:\n{steps_desc}")
#                 if named_steps:
#                     parts.append(f"Apply these named operations:\n{named_desc}")
#                 if compute_desc:
#                     parts.append(f"Then use execute_python_code to calculate: {compute_desc}")
#                 # Include step-to-field mapping so executor can name fields in friendly_response
#                 if step_field_map:
#                     parts.append(
#                         "Step-to-field mapping (use these field names in your friendly_response):\n"
#                         + "\n".join(step_field_map)
#                     )
#                 parts.append(f"User query: {query}")
#                 exec_prompt = "\n\n".join(parts)
#             else:
#                 exec_prompt = query

#             with observe_step(
#                 name="excel-chat:decision:short_circuit",
#                 input={"path": "executor", "task_type": plan.task_type},
#             ) as decision_span:
#                 if decision_span:
#                     decision_span.record(decision="run_executor")
#                 with timed("executor", timings):
#                     with observe_step(
#                         name="excel-chat:step:executor",
#                         input={"task_type": plan.task_type},
#                     ) as executor_span:
#                         exec_result = await _run_with_fallback(
#                             build_executor_agent, exec_prompt, sheets, sheet_metas, deps=deps
#                         )
#                         if executor_span:
#                             executor_span.record_usage(
#                                 exec_result.usage, timings.get("executor", 0) * 1000
#                             )
#                             stage_usages.append(exec_result.usage)
#             execution: ExecutionResult = exec_result.output
#             print("✅ Execution:", json.dumps(execution.model_dump(), indent=2))
#             _emit("execution", {"step_results": execution.step_results, "final_answer": execution.final_answer, "explanation": execution.explanation})

#             # Layer 3: Post-execution structured key derivation
#             try:
#                 from result_cache import cache_step_results
#                 cache_step_results(user_id, plan, execution)
#             except Exception as e:
#                 print(f"⚠️ Post-execution caching failed: {e}")

#             # Step 3: Use executor's friendly_response (merged responder)
#             friendly = inject_disclaimer(
#                 execution.friendly_response or _format_simple_response(query, plan, execution) or ""
#             )
#             if friendly:
#                 print(f"Response: {friendly[:100]}...")
#             else:
#                 print("⚠️ No friendly response generated")
#             _emit("friendly", {"response": friendly})

#             total = sum(timings.values())
#             print(f"Pipeline total: {total:.2f}s | {timings}")
#             if trace_span:
#                 _tokens_in = sum(
#                     getattr(u, "input_tokens", 0) or 0 for u in stage_usages
#                 )
#                 _tokens_out = sum(
#                     getattr(u, "output_tokens", 0) or 0 for u in stage_usages
#                 )
#                 _cost = None
#                 _costs = [
#                     float(c) for u in stage_usages
#                     if (c := getattr(u, "cost", None)) is not None
#                 ]
#                 if _costs:
#                     _cost = sum(_costs)
#                 trace_span.record_totals(
#                     tokens_in=_tokens_in,
#                     tokens_out=_tokens_out,
#                     cost_usd=_cost,
#                     time_ms=total * 1000,
#                 )
#             return {
#                 "answer": execution.model_dump(),
#                 "plan": plan.model_dump(),
#                 "task_type": plan.task_type,
#                 "friendly_response": friendly,
#                 "timings": timings,
#             }


# ============================================================================
# OLD generate_user_friendly_response (retired — references build_responder_agent above)
# ============================================================================

# async def generate_user_friendly_response(
#     llm_client: Any | None, original_query: str, json_result: Any
# ) -> str:
#     """
#     Generate a user-friendly response from the JSON result.
#     llm_client is kept for backward compatibility but ignored.
#     """
#     responder = build_responder_agent()
#     prompt = f"""User's Question: {original_query}
# 
#     Calculated Results:
#     {json.dumps(json_result, indent=2)}
# 
#     Provide a clear, natural language response."""
#     result = await _run_with_fallback(build_responder_agent, prompt)
#     return inject_disclaimer(result.output)

# ============================================================================
# Legacy helper functions (preserved for backward compatibility)
# ============================================================================

def executing_plan_from_json(df: pd.DataFrame, json_str: str) -> dict[str, Any]:
    """
    Legacy deterministic plan executor. Kept for backward compatibility.
    """
    try:
        parsed = json.loads(json_str)
        print("Parsed JSON:", parsed)

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
            print(f"Processing step {step_name}: {instruction}")

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
    print(f"Attempting to retrieve: {items}")
    if len(items) == 2:
        col, year = items[0].strip(), items[1].strip()
        print(
            f"Looking for: '{col}' in index ({col in df.index}), "
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
# ============================================================================
# OLD pre-populate helpers (retired — see agent.py _try_deterministic_lookup)
# ============================================================================

# def _prepopulate_retrievals(plan: QueryPlan, deps: PipelineDeps) -> dict[str, Any]:
#     """Execute every retrieve step in pure Python (no LLM).
# 
#     Optimization 2: collapses N sequential LLM round-trips into a single
#     Python pass before the executor runs. The executor then only handles
#     computation + friendly-response generation.
# 
#     The returned dict maps step name → retrieved value (a scalar string for
#     single-year retrievals or a parsed dict for multi-year). Per-step failures
#     are captured as ``"ERROR: ..."`` strings so downstream code can decide.
#     """
#     from types import SimpleNamespace
# 
#     pre_populated: dict[str, Any] = {}
#     if not plan.plan:
#         return pre_populated
# 
#     # ``retrieve_values`` only touches ``ctx.deps`` — a
#     # SimpleNamespace is sufficient.
#     ctx = SimpleNamespace(deps=deps)
# 
#     for name, step in plan.plan.items():
#         args = step.args or []
#         try:
#             if step.action == "retrieve":
#                 # Cross-sheet: ["Field", "Year1", "Year2", ...] → args[1] is a year
#                 # Specific sheet: ["Sheet", "Field", "Year1", ...] → args[1] is the field
#                 if len(args) < 2:
#                     pre_populated[name] = "ERROR: retrieve needs at least 2 args"
#                     continue
#                 if _looks_like_year(args[1]):
#                     field = args[0]
#                     years = args[1:]
#                     with observe_step(
#                         name="excel-chat:tool:retrieve_values",
#                         input={"field": field, "years": years, "sheet": ""},
#                     ) as retrieve_span:
#                         raw = retrieve_values(ctx, field, years, sheet="")
#                         if retrieve_span:
#                             retrieve_span.set_output(raw)
#                 else:
#                     sheet = args[0]
#                     field = args[1]
#                     years = args[2:]
#                     with observe_step(
#                         name="excel-chat:tool:retrieve_values",
#                         input={"field": field, "years": years, "sheet": sheet},
#                     ) as retrieve_span:
#                         raw = retrieve_values(ctx, field, years, sheet=sheet)
#                         if retrieve_span:
#                             retrieve_span.set_output(raw)
#                 # Multi-year returns JSON; single-year returns a plain string
#                 if len(years) > 1:
#                     try:
#                         pre_populated[name] = json.loads(raw)
#                     except (json.JSONDecodeError, TypeError):
#                         pre_populated[name] = raw
#                 else:
#                     pre_populated[name] = raw
#             else:
#                 # Named ops / compute can't be pre-populated — leave for executor.
#                 continue
#         except Exception as e:
#             print(f"⚠️ Pre-populate failed for {name}: {e}")
#             pre_populated[name] = f"ERROR: {type(e).__name__}: {e}"
# 
#     return pre_populated
# 
# 
# def _format_pre_populated_for_prompt(pre_populated: dict[str, Any]) -> str:
#     """Render pre-populated values as a human-readable block for the executor.
# 
#     Format:
#         Pre-computed values (use these directly, do NOT call retrieve):
# 
#         step1: 1500.0
#         step2: 1200.0
#         step3: {"2018": 1500.0, "2019": 1200.0}
#     """
#     if not pre_populated:
#         return ""
#     lines = [
#         "Pre-computed values (use these directly — do NOT call retrieve_values "
#         "again):"
#     ]
#     for name, value in pre_populated.items():
#         lines.append(f"  {name}: {value}")
#     return "\n".join(lines)
# 
# 
# def _plan_is_pure_retrieve(plan: QueryPlan) -> bool:
#     """True iff every step is a retrieve (no compute, no named ops)."""
#     if not plan.plan:
#         return False
#     return all(
#         step.action == "retrieve"
#         for step in plan.plan.values()
#     )
# 
