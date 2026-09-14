"""Single query agent for Excel-chat.

Builds the Pydantic AI agent that plans (write_plan), executes (tools),
and produces structured output (ExecutionResult). Plan and output are
both semantically validated.

Pipeline orchestration (build_query_pipeline, fallback, short-circuit)
lives in pipeline.py. Shared models live in models.py.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Instrumentation, ProcessHistory, Thinking
from pydantic_ai.messages import ModelMessage, ToolReturnPart

from sheet_metadata import SheetMeta
from guardrails import GUARDRAIL_SYSTEM_PROMPT
from tools import (
    PipelineDeps,
    retrieve_values,
    execute_python_code,
    analyze_sheet,
    find_missing_data,
    discover_column_types,
    get_sheet_shape,
    group_summary_stats,
    create_pivot_table,
    crosstab_analysis,
    drop_missing_columns,
    convert_to_numerical,
    correlation_analysis,
    get_sheet_head,
    get_sheet_info,
    value_counts_analysis,
    deduplicate_rows,
    get_sheet_dtypes,
    _prepare_retrieve_values_tool,
)
from observability import init_observability
from models import (
    QueryPlan,
    ExecutionResult,
    build_openrouter_model,
)

# Initialise Langfuse / Pydantic AI instrumentation before any Agent is built.
init_observability()


# ============================================================================
# Plan Validation (semantic — schema is enforced by Pydantic)
# ============================================================================

def validate_plan_semantics(plan: QueryPlan, sheet_metas: list[SheetMeta]) -> list[str]:
    """Semantic validation of a plan against the sheet catalog.

    Layer 1 (schema) is handled automatically by pydantic: ``write_plan``'s
    ``plan: QueryPlan`` argument is schema-validated before the tool runs, so
    unknown actions / malformed args never reach this function.

    This function closes the gap the old planner had: a typo'd field name
    ("Revenu") or invalid year ("2019x") used to pass schema validation and
    only fail at execution time. Here the plan is rejected *before* any
    retrieval runs, with the valid options listed so the model can fix it.

    Returns a list of human-readable error strings (empty = plan is valid).
    """
    errors: list[str] = []
    valid_fields = {f for m in sheet_metas for f in m.fields}
    valid_years = {str(y) for m in sheet_metas for y in m.years}
    sheet_names = {m.sheet_name for m in sheet_metas}
    steps = plan.plan or {}
    step_ids = set(steps.keys())

    # ---- Check 0: items format (retrieve_numbers) ----
    if plan.items:
        for i, item in enumerate(plan.items):
            parts = [p.strip() for p in item.split(",")]
            if len(parts) == 2:
                field, year = parts
            elif len(parts) == 3:
                sheet, field, year = parts
                if sheet not in sheet_names:
                    errors.append(
                        f"item {i+1}: unknown sheet '{sheet}'. Valid sheets: {sorted(sheet_names)}"
                    )
            else:
                errors.append(
                    f"item {i+1}: '{item}' must be 'FieldName, Year' or "
                    f"'SheetName, FieldName, Year' (got {len(parts)} parts)."
                )
                continue
            if field not in valid_fields:
                errors.append(
                    f"item {i+1}: unknown field '{field}'. "
                    f"Use exact field names ({_sample_from(valid_fields, field)})."
                )
            if year not in valid_years:
                errors.append(
                    f"item {i+1}: unknown year '{year}'. Valid years: {sorted(valid_years)}"
                )

    # ---- Check 1: step validation (perform_calculations) ----
    seen: set[str] = set()
    # Track retrieve steps by field for multi-year batching check
    retrieve_fields: dict[str, list[str]] = {}  # field → list of step_ids

    for sid, step in steps.items():
        if step.action == "retrieve":
            # args = [maybe SheetName, FieldName, years...]
            if not step.args:
                errors.append(f"{sid}: 'retrieve' requires args ['FieldName', 'Year', ...]")
                seen.add(sid)
                continue
            maybe_sheet = step.args[0]
            if maybe_sheet in sheet_names:
                field = step.args[1] if len(step.args) > 1 else ""
                years = step.args[2:]
            else:
                field = step.args[0]
                years = step.args[1:]
            if field not in valid_fields:
                errors.append(
                    f"{sid}: unknown field '{field}'. Use exact field names from the sheet catalog ({_sample_from(valid_fields, field)})."
                )
            bad_years = [y for y in years if y not in valid_years]
            if bad_years:
                errors.append(f"{sid}: unknown year(s) {bad_years}. Valid years: {sorted(valid_years)}")
            # Track for multi-year batching check
            if field and years:
                retrieve_fields.setdefault(field, []).append(sid)
        elif step.action != "compute":
            # Named operation — validate arg count
            expected = _OP_ARG_COUNTS.get(step.action)
            if expected is not None:
                actual = len(step.args)
                min_args, max_args = expected
                if actual < min_args or actual > max_args:
                    range_str = (
                        f"exactly {min_args}" if min_args == max_args
                        else f"{min_args}-{max_args}"
                    )
                    errors.append(
                        f"{sid}: '{step.action}' expects {range_str} arg(s), got {actual}. "
                        f"Args: {step.args}"
                    )
            # Args must reference prior steps or be numeric literals
            for ref in step.args:
                if ref in step_ids:
                    # Check for forward references (defined later)
                    if ref not in seen:
                        errors.append(f"{sid}: references '{ref}' which is defined later — steps must only use prior results")
                    continue
                try:
                    float(ref)
                except (TypeError, ValueError):
                    errors.append(
                        f"{sid}: arg '{ref}' must reference a prior step (e.g. 'step1') or be a literal number"
                    )
        seen.add(sid)

    # ---- Check 2: multi-year batching (anti-pattern detection) ----
    for field, sids in retrieve_fields.items():
        if len(sids) > 1:
            errors.append(
                f"Multiple retrieve steps for '{field}': {sids}. "
                f"Combine into ONE retrieve step with multiple years: "
                f"['{field}', 'Year1', 'Year2', ...]."
            )

    return errors


# Arg count constraints per named operation: (min, max)
# Unary: exactly 1. Binary: exactly 2. Ternary: exactly 3. N-ary: 2 or more.
_OP_ARG_COUNTS: dict[str, tuple[int, int]] = {
    # Unary (1 arg)
    "sqrt": (1, 1), "abs": (1, 1), "negate": (1, 1), "exp": (1, 1),
    # Binary (2 args)
    "subtract": (2, 2), "divide": (2, 2), "return_percentage": (2, 2),
    "power": (2, 2), "log": (2, 2), "yoy_growth": (2, 2),
    "ratio": (2, 2), "percentage_change": (2, 2), "difference": (2, 2),
    # Ternary (3 args)
    "cagr": (3, 3),
    # N-ary (2+ args)
    "add": (2, 99), "multiply": (2, 99), "max": (2, 99),
    "min": (2, 99), "average": (2, 99), "median": (2, 99), "stdev": (2, 99),
}


def _sample_from(values: set[str], hint: str) -> str:
    """Pick the closest catalog value to *hint* for a helpful error message."""
    import difflib
    close = difflib.get_close_matches(hint, values, n=1, cutoff=0.6)
    if close:
        return f"did you mean '{close[0]}'?"
    return f"e.g. {sorted(values)[:5]}"


# ============================================================================
# Output Validation (mirrors validate_plan_semantics)
# ============================================================================

def validate_execution_result(
    execution: ExecutionResult,
    plan: QueryPlan | None,
    sheet_metas: list[SheetMeta],
) -> list[str]:
    """Semantic validation of the agent's structured output.

    Mirrors validate_plan_semantics: the plan is validated before execution
    (schema + catalog), and the output is validated after execution. Returns
    a list of human-readable error strings (empty = output is valid).

    Checks:
      1. step_results completeness — every step id in the accepted plan must
         appear in step_results (no missing/empty dict).
      2. final_answer consistency — final_answer must not be None/empty, and
         if the plan has a single terminal step, final_answer should match
         that step's result.
      3. friendly_response field names — friendly_response must reference at
         least one actual field name from the sheet catalog (rejects generic
         "the first series" phrasing the old executor prompt warned about).

    When *plan* is None (advice/EDA runs with no write_plan), only check 3 is
    applied — steps and final_answer are not plan-bound.
    """
    errors: list[str] = []
    valid_fields = {f for m in sheet_metas for f in m.fields}
    step_results = execution.step_results or {}
    final = execution.final_answer
    friendly = execution.friendly_response or ""
    friendly_lower = friendly.lower()

    # Markers that the agent explicitly reported missing/unavailable data.
    # When a step failed because the SOURCE has no data (not an agent error),
    # the plan is acceptable as long as the agent told the user.
    _MISSING_DATA_MARKERS = (
        "not available", "no data", "unavailable", "missing",
        "could not be retrieved", "cannot be retrieved", "not found",
        "is not present", "are not present", "no value",
    )
    reported_missing = any(m in friendly_lower for m in _MISSING_DATA_MARKERS)

    def _is_source_error(val: Any) -> bool:
        """True when a step result indicates the source data is unavailable."""
        return isinstance(val, str) and (
            val.startswith("ERROR:") or not val.strip()
        )

    # ---- Check 1: step_results completeness ----
    if plan is not None and plan.plan:
        missing_steps = [sid for sid in plan.plan if sid not in step_results]
        if missing_steps:
            errors.append(
                f"step_results is missing these plan steps: {missing_steps}. "
                "Every step in the plan must have a result in step_results."
            )
        empty_steps = [
            sid for sid in plan.plan
            if sid in step_results and _is_source_error(step_results[sid])
        ]
        # Empty/ERROR step results are acceptable ONLY when the agent
        # explicitly told the user the data is missing. Otherwise they
        # indicate the agent silently dropped a step.
        if empty_steps and not reported_missing:
            errors.append(
                f"step_results has empty/ERROR values for steps: {empty_steps}. "
                "If the source data is genuinely unavailable, keep the ERROR "
                "value AND say so in friendly_response (e.g. 'X is not "
                "available for year Y'). Otherwise re-run the retrieval."
            )
    elif plan is not None and plan.items:
        # retrieve_numbers with items → expect step1, step2, ...
        expected = [f"step{i+1}" for i in range(len(plan.items))]
        missing = [s for s in expected if s not in step_results]
        if missing:
            errors.append(
                f"step_results is missing item steps: {missing}. "
                f"Expected {expected} for the {len(plan.items)} items in the plan."
            )

    # ---- Check 2: final_answer consistency ----
    if plan is not None and plan.plan:
        if final is None or (isinstance(final, str) and not final.strip()):
            # An empty final_answer is acceptable only when the agent
            # reported the data as missing (all sources empty).
            if not reported_missing:
                errors.append(
                    "final_answer is empty/None. Set it to the computed or "
                    "retrieved value — or, if the source data is unavailable, "
                    "say so explicitly in friendly_response."
                )
        else:
            # Find the terminal step: the last non-retrieve step (the
            # computation that produces the final value). If ALL steps are
            # retrieves (the model computed via tool calls outside the plan),
            # skip this check — final_answer is the tool-computed result and
            # won't match any retrieve step's output.
            terminal_sid: str | None = None
            for sid, step in reversed(list(plan.plan.items())):
                if step.action != "retrieve":
                    terminal_sid = sid
                    break
            if terminal_sid and terminal_sid in step_results:
                step_val = step_results[terminal_sid]
                # Normalize for comparison: strip formatting (%, $, commas)
                # and compare as floats when possible so "11.17%" matches
                # 11.170279808529374.
                def _norm_num(v: Any) -> float | None:
                    s = str(v).strip().rstrip("%").replace(",", "").replace("$", "")
                    try:
                        return float(s)
                    except (ValueError, TypeError):
                        return None
                # Extract the first number from a string like "-9.24 percentage
                # points" or "14.70%" — used when step_val is a human-readable
                # string rather than a bare number.
                def _extract_num(v: Any) -> float | None:
                    import re
                    m = re.search(r"-?\d+\.?\d*", str(v))
                    return float(m.group()) if m else None
                fn_num = _norm_num(final)
                sv_num = _norm_num(step_val)
                # If step_val is a string that _norm_num can't parse directly
                # (e.g. "-9.24 percentage points"), try extracting the number.
                if sv_num is None and isinstance(step_val, str):
                    sv_num = _extract_num(step_val)
                mismatch = False
                if fn_num is not None and sv_num is not None:
                    # Both numeric — compare with small tolerance for rounding
                    if abs(fn_num - sv_num) > max(abs(sv_num) * 0.01, 0.01):
                        mismatch = True
                elif isinstance(final, str) and sv_num is not None:
                    # final_answer is a human-readable string, step result is
                    # numeric. Accept if the numeric value appears as a
                    # substring (e.g. "grew at 23.94%" contains 23.94).
                    # Also accept percentage/fraction equivalence (step returns
                    # 0.0924, model writes "9.24%") and comma-formatted
                    # variants ("7,966.91" matches 7966.91).
                    abs_sv = abs(sv_num)
                    pct_sv = sv_num * 100
                    abs_pct_sv = abs(pct_sv)
                    formatted_variants = [
                        str(sv_num), str(abs_sv),
                        f"{sv_num:.1f}", f"{sv_num:.2f}",
                        f"{abs_sv:.1f}", f"{abs_sv:.2f}",
                        f"{sv_num:,.1f}", f"{sv_num:,.2f}",
                        f"{abs_sv:,.1f}", f"{abs_sv:,.2f}",
                        # Percentage/fraction equivalence: 0.0924 ↔ "9.24%"
                        f"{pct_sv:.1f}", f"{pct_sv:.2f}",
                        f"{abs_pct_sv:.1f}", f"{abs_pct_sv:.2f}",
                        f"{pct_sv:,.1f}", f"{pct_sv:,.2f}",
                        f"{abs_pct_sv:,.1f}", f"{abs_pct_sv:,.2f}",
                    ]
                    # Also strip commas from final_answer for comparison
                    final_nocomma = final.replace(",", "")
                    if not any(v in final or v in final_nocomma for v in formatted_variants):
                        mismatch = True
                elif isinstance(final, str) and isinstance(step_val, (dict, str)):
                    # Both non-numeric — check if final_answer contains
                    # key values from the step result. For dicts, check both
                    # numeric and string values (e.g. the "more_stable" field
                    # name appears in the human-readable answer).
                    # Note: step_val may be a JSON string (flattened by
                    # _flatten_value in models.py), so try to parse it.
                    parsed_step = step_val
                    if isinstance(step_val, str):
                        try:
                            import json as _json
                            parsed_step = _json.loads(step_val)
                        except (ValueError, TypeError):
                            parsed_step = step_val
                    step_str = str(step_val)
                    final_nocomma = final.replace(",", "")
                    if step_str not in final and str(final).strip() != step_str.strip():
                        if isinstance(parsed_step, dict):
                            # Dict step results are complex multi-value outputs
                            # (e.g. {wages_cv: 0.078, employer_cv: 0.077, ...}).
                            # Try to find any value (or its percentage/comma
                            # variant) in the final_answer. If none match but
                            # the final_answer is a non-empty qualitative
                            # statement, accept it — the step results are
                            # already in step_results and the model has seen
                            # them. Forcing a retry here just wastes a round-trip.
                            vals_in_step: list[str] = []
                            for v in parsed_step.values():
                                if isinstance(v, str) and len(v) >= 3:
                                    vals_in_step.append(v)
                                elif isinstance(v, (int, float)):
                                    abs_v = abs(v)
                                    pct_v = v * 100
                                    vals_in_step.extend([
                                        str(v), f"{v:.1f}", f"{v:.2f}",
                                        f"{abs_v:.1f}", f"{abs_v:.2f}",
                                        f"{v:,.1f}", f"{v:,.2f}",
                                        f"{pct_v:.1f}", f"{pct_v:.2f}",
                                    ])
                            if not any(v in final or v in final_nocomma for v in vals_in_step):
                                # Qualitative answer for a dict step result —
                                # accept it rather than forcing a retry.
                                pass
                        else:
                            mismatch = True
                if mismatch and not isinstance(final, (dict, list)):
                    errors.append(
                        f"final_answer ({final}) does not match the terminal "
                        f"step {terminal_sid} result ({step_val}). "
                        "final_answer should equal or contain the last step's output."
                    )

    # ---- Check 3: friendly_response field names ----
    if friendly:
        referenced = [f for f in valid_fields if len(f) >= 4 and f.lower() in friendly_lower]
        if not referenced:
            # Check if any field name appears as a substring (case-insensitive)
            # even shorter ones (>= 3 chars) to avoid matching common words.
            referenced = [f for f in valid_fields if len(f) >= 3 and f.lower() in friendly_lower]
            # Filter out very common short words that might false-positive
            common_words = {"the", "and", "for", "was", "are", "all", "new", "old", "sum", "avg", "max", "min"}
            referenced = [f for f in referenced if f.lower() not in common_words]
        if not referenced:
            sample = sorted(valid_fields)[:8]
            errors.append(
                "friendly_response does not reference any actual field name from the "
                f"sheet catalog. Use real field names (e.g. {sample}) instead of generic "
                "phrases like 'the first series' or 'the value'."
            )
    else:
        errors.append(
            "friendly_response is empty. Provide a natural-language answer using actual "
            "field names from the sheet catalog."
        )

    return errors


# ============================================================================
# Agent Builder
# ============================================================================

def _build_sheet_context(sheet_metas: list[SheetMeta]) -> tuple[str, str]:
    """Build the multi-sheet catalog block for the system prompt.

    Returns (sheet_context, cross_sheet_note):
      - sheet_context: one entry per sheet with name, file, fields, years, description
      - cross_sheet_note: tells the model which sheets share a schema group
        (so it knows cross-sheet calculations are possible)
    """
    sheet_context_parts = []
    groups: dict[str, list[str]] = {}
    for meta in sheet_metas:
        group_note = f" (schema group: {meta.schema_group})" if meta.schema_group and meta.schema_group != "unique" else ""
        desc_note = f"\n      Description: {meta.combined_description}" if meta.combined_description != "No description available." else ""
        sheet_context_parts.append(
            f"  - Sheet '{meta.sheet_name}' (file: {meta.file_name}){group_note}\n"
            f"      Fields: {', '.join(meta.fields[:20])}\n"
            f"      Years: {', '.join(meta.years)}{desc_note}"
        )
        if meta.schema_group and meta.schema_group != "unique":
            groups.setdefault(meta.schema_group, []).append(meta.sheet_name)
    sheet_context = "\n".join(sheet_context_parts)

    cross_sheet_note = ""
    if groups:
        group_descs = [f"  - {', '.join(sheets_list)}" for sheets_list in groups.values()]
        cross_sheet_note = (
            "\n    Cross-sheet calculations are possible for sheets with the same schema group:\n"
            + "\n".join(group_descs)
            + "\n    When retrieving from a schema group, omit the sheet name to search all sheets in that group."
        )
    return sheet_context, cross_sheet_note


def n_steps_label(plan: QueryPlan) -> str:
    if plan.plan:
        return f"{len(plan.plan)} steps"
    if plan.items:
        return f"{len(plan.items)} items"
    return "no steps"


# ============================================================================
# Dynamic Tool Selection + Intent Routing
# ============================================================================
# The query is classified into one of four intents BEFORE the agent is
# configured. This determines tool selection, thinking effort, and request
# limits — giving each intent a pipeline-like feel instead of a flat loop.
#
#   retrieve_numbers   — simple lookups (1 field, 1 year). Lean tools, no
#                        thinking, 50 request limit.
#   perform_calculations — math/comparison/trend. Lean tools, no thinking,
#                        50 request limit.
#   give_advice        — recommendations, strategy, "what should I do".
#                        Lean tools + Thinking(effort="medium"), 3 request
#                        limit to prevent bill spiraling on open-ended
#                        reasoning loops.
#   eda                — data quality, distributions, correlations. All 17
#                        tools, no thinking, 50 request limit.

# Keywords that signal an EDA / data-quality / distribution question.
_EDA_KEYWORDS = (
    "data quality", "missing data", "missing values", "null", "nan",
    "distribution", "distribute", "histogram", "outlier",
    "correlation", "correlate", "covariance",
    "summary stat", "descriptive", "describe",
    "pivot", "crosstab", "cross-tab", "cross tab",
    "data type", "dtype", "dtypes", "column type",
    "deduplicate", "duplicate", "duplicates",
    "drop missing", "convert to numerical", "numerical",
    "shape", "head", "info", "value count", "value_counts",
    "explore", "eda", "exploratory",
    "clean", "cleaning", "preprocess",
    "skew", "kurtosis", "variance", "quartile", "percentile",
)

# Keywords that signal an advice / recommendation / strategy question.
# These need reasoning (Thinking) because the answer requires synthesizing
# multiple data points into a recommendation — not just computing a number.
_ADVICE_KEYWORDS = (
    "should i", "what should", "recommend", "recommendation",
    "suggest", "suggestion", "advice", "advise",
    "strategy", "strategic", "improve", "improvement",
    "how can i", "how should", "what would you",
    "keep on track", "on track", "stay on track",
    "optimize", "optimization", "best way",
    "prioritize", "priority", "focus on",
    "concern", "concerning", "risk", "risky",
    "opportunity", "opportunities",
    "insight", "insights", "takeaway", "takeaways",
    "what about", "what if",
)

# Keywords that signal a calculation / comparison / trend question.
_CALC_KEYWORDS = (
    "growth", "rate", "ratio", "percent", "average", "compare",
    "comparison", "difference", "cagr", "trend", "increase",
    "decrease", "change", "stability", "stable", "highest", "lowest",
    "maximum", "minimum", "max", "min", "median", "stdev", "fraction",
    "sum of", "total of", "between", "vs", "versus", "by how much",
    "how much did", "analyze", "volatile", "volatility",
)


def _classify_intent(query: str) -> str:
    """Classify query intent before agent configuration.

    Returns one of: "eda", "give_advice", "perform_calculations",
    "retrieve_numbers".

    Order matters: EDA is checked first (data-quality terms are
    distinctive), then advice (recommendation terms are distinctive),
    then calculations (math keywords), then default to retrieve_numbers.
    """
    import re
    q = query.lower()

    # EDA — use word-boundary matching for short keywords like "nan", "null"
    # to avoid false positives on words like "financial" (contains "nan")
    for kw in _EDA_KEYWORDS:
        if len(kw) <= 4:
            if re.search(rf"\b{re.escape(kw)}\b", q):
                return "eda"
        elif kw in q:
            return "eda"

    # Advice — recommendations, strategy, "what should I do"
    if any(kw in q for kw in _ADVICE_KEYWORDS):
        return "give_advice"

    # Calculations — math/comparison/trend keywords
    if any(kw in q for kw in _CALC_KEYWORDS):
        return "perform_calculations"

    # Default — simple lookup
    return "retrieve_numbers"


def _is_eda_query(query: str) -> bool:
    """Backward-compatible EDA check (delegates to _classify_intent)."""
    return _classify_intent(query) == "eda"


# Context-window management: clear old tool results once the history grows
# past _HISTORY_CLEAR_THRESHOLD messages, keeping the most recent
# _KEEP_RECENT_TOOL_RESULTS tool results intact.
_HISTORY_CLEAR_THRESHOLD = 24
_KEEP_RECENT_TOOL_RESULTS = 4


def clear_old_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor: replace old tool-result payloads with placeholders.

    Once the history exceeds _HISTORY_CLEAR_THRESHOLD messages, the content of
    older ToolReturnParts is replaced with a short note. The most recent
    _KEEP_RECENT_TOOL_RESULTS tool results stay intact (the agent is usually
    still reasoning over them). Tool-call/result pairing is preserved — only
    the payload shrinks — so providers accept the history. By the time the
    agent writes the final ExecutionResult, step_results and final_answer
    carry everything needed; old raw tool outputs are dead weight.
    """
    tool_returns = [
        (i, part) for i, msg in enumerate(messages)
        if getattr(msg, "parts", None)
        for part in msg.parts
        if isinstance(part, ToolReturnPart)
    ]
    if len(messages) <= _HISTORY_CLEAR_THRESHOLD or len(tool_returns) <= _KEEP_RECENT_TOOL_RESULTS:
        return messages
    cutoff = len(tool_returns) - _KEEP_RECENT_TOOL_RESULTS
    for _idx, (_msg_i, part) in enumerate(tool_returns[:cutoff]):
        part.content = (
            f"[{part.tool_name} result cleared to save context — "
            f"value already recorded in step_results]"
        )
    return messages


def build_query_agent(
    sheet_metas: list[SheetMeta],
    model_name: str | None = None,
    query: str = "",
) -> Agent[PipelineDeps, ExecutionResult]:
    """Build the single query agent.

    One agent covers the full query lifecycle — plan, execute, observe, output:
      1. PLAN:   calls ``write_plan`` with a schema-validated QueryPlan
                 (Literal action enum + semantic validation against the sheet
                 catalog — malformed plans are rejected before execution).
      2. ACT:    calls ``retrieve_values`` / ``execute_python_code`` / EDA tools.
      3. OBSERVE: tool results return into context; the model reasons between
                 calls and updates step status.
      4. OUTPUT: structured ``ExecutionResult`` (step_results + final_answer +
                 friendly_response) — synthesis is the final structured output.

    Tool selection: when *query* looks like an EDA / data-quality / distribution
    question, all 17 EDA tools are registered. Otherwise only the 4 core tools
    (retrieve_values, execute_python_code, write_plan, update_step_status) are
    used — this reduces context bloat and tool-selection errors on smaller
    models like DeepSeek V4 Flash.

    Note: sheets and the SSE callback are passed at run time via PipelineDeps
    (ctx.deps), not at build time.
    """
    model = build_openrouter_model(model_name)
    sheet_context, cross_sheet_note = _build_sheet_context(sheet_metas)
    all_fields = sorted({f for meta in sheet_metas for f in meta.fields})
    all_years = sorted({str(y) for meta in sheet_metas for y in meta.years})
    retrieve_t = Tool(retrieve_values, prepare=_prepare_retrieve_values_tool)

    # ---- Dynamic tool selection + intent-based routing ----
    intent = _classify_intent(query) if query else "eda"  # default: all tools
    is_eda = intent == "eda"
    is_advice = intent == "give_advice"
    eda_tools = [
        analyze_sheet,
        find_missing_data,
        discover_column_types,
        get_sheet_shape,
        group_summary_stats,
        create_pivot_table,
        crosstab_analysis,
        drop_missing_columns,
        convert_to_numerical,
        correlation_analysis,
        get_sheet_head,
        get_sheet_info,
        value_counts_analysis,
        deduplicate_rows,
        get_sheet_dtypes,
    ]
    if is_eda:
        # EDA queries use EDA tools directly (no plan needed)
        tools = [retrieve_t, execute_python_code] + eda_tools
        tool_note = "You have EDA tools available for data quality, distributions, and correlations."
    else:
        # Calculation/retrieval queries: ONLY write_plan (registered below).
        # The deterministic executor in write_plan handles retrieve/compute.
        # execute_python_code is kept for COMPUTE_NEEDED fallback.
        tools = [execute_python_code]
        tool_note = "Use write_plan to plan and execute. Results come back in the tool response."
    # write_plan is registered via @agent.tool below
    total_tools = len(tools) + 1
    print(f"🔧 Intent: {intent} | Tools: {total_tools} ({'EDA' if is_eda else 'core-only'}) | Thinking: {'medium' if is_advice else 'off'} — {tool_note}", flush=True)

    system_prompt = f"""You are a financial data analysis agent that plans, executes, and answers.

    Available sheets and their data:
    {sheet_context}
    {cross_sheet_note}

    All available fields across sheets: {', '.join(all_fields)}
    All available years across sheets: {', '.join(all_years)}

    ## Workflow (follow this order):

    1. PLAN — call `write_plan` with a structured QueryPlan:
       - task_type: "retrieve_numbers" (simple lookups), "perform_calculations"
         (any math/comparison), "give_advice" (recommendations), or "other".
       - For perform_calculations: numbered steps using these actions:
           * retrieve — args = ["FieldName", "Year1", "Year2", ...] (searches all
             sheets) or ["SheetName", "FieldName", "Year1", ...] (specific sheet).
             ALWAYS use exact field names and years from the lists above.
           * Named math ops — args reference prior steps or literal numbers:
             Unary (1 arg): sqrt, abs, negate, exp
             Binary (2 args): subtract, divide, return_percentage, power, log,
               yoy_growth, ratio, percentage_change, difference
             Ternary (3 args): cagr [end_value, start_value, num_years]
             N-ary (2+ args): add, multiply, max, min, average, median, stdev
           * compute — for complex calculations that named ops can't express.
             Put the actual Python code in the `description` field (must contain
             a `return` statement). Retrieved values from prior steps are
             available as variables named step1, step2, etc.
           Use step1, step2, step3 as keys (NOT "steps"). Each step needs a
           short `description` for the UI, e.g.
           "Retrieving social security benefits 2017-2021".
       - TREND / ALL-YEARS queries ("across all years", "over time", "trend"):
         use this exact 2-step structure — NOT one step per year:
           step1: retrieve ["FieldName", <every year listed in the catalog>]
           step2: compute "<per-year calculation, e.g. margin = (revenue-expense)/revenue>"
       - For retrieve_numbers: items = ["FieldName, Year", ...]
       - The plan is VALIDATED: unknown fields/years, unknown actions, or
         forward step references are REJECTED — you will be asked to fix them.
         Use exact names from the catalog to avoid rejection.

    2. RESULTS — when write_plan returns, ALL retrieve and named-op steps
       have been executed automatically. The results are in the tool response.
       IMPORTANT: Do NOT call retrieve_values or execute_python_code again —
       the data is already in the write_plan results. Use those results
       directly to produce your ExecutionResult in step 3.
       Only call execute_python_code if a step is marked "COMPUTE_NEEDED".
       - For EDA questions (data quality, distributions, correlations), use the
         EDA tools directly instead of writing a step plan. {tool_note}

    3. ANSWER — return ExecutionResult with:
       - step_results: EVERY step's result (never an empty dict)
       - final_answer: the computed/retrieved answer (a concise string)
       - explanation: how the answer was derived
       - friendly_response: a NATURAL-LANGUAGE sentence using ACTUAL field
         names. This is what the user sees — write it as if explaining to a
         colleague, NOT as raw data.
         WRONG: {{"cv_wages": 0.078, "conclusion": "more stable"}}
         WRONG: "The first series grew at 25.4%".
         RIGHT: "Employers' social contributions were more stable than wages
         and salaries (coefficient of variation: 0.077 vs 0.079). The
         difference is negligible at 0.002."
         NEVER put raw JSON, a dict, or a compute result directly into
         friendly_response. Always synthesize into plain English.
       When retrieve returns multiple sheet values, format is "SheetName: value; ...".
       If a retrieval fails, note it and continue with what you have.

    {GUARDRAIL_SYSTEM_PROMPT}
    """

    # ---- Capabilities: advice gets Thinking, others don't ----
    capabilities: list[Any] = [
        Instrumentation(),        # Langfuse OTel tracing
        ProcessHistory(clear_old_tool_results),  # context-window management
    ]
    if is_advice:
        capabilities.insert(0, Thinking(effort="medium"))

    agent = Agent(
        model,
        deps_type=PipelineDeps,
        output_type=ExecutionResult,
        system_prompt=system_prompt,
        tools=tools,
        model_settings={"temperature": 0.1},
        retries=2,
        capabilities=capabilities,
    )

    # ---- Plan tools: code-enforced planning (schema + semantic validation) ----

    @agent.tool
    async def write_plan(ctx: RunContext[PipelineDeps], plan: QueryPlan) -> str:
        """Write the execution plan. The plan is then executed deterministically
        (no model calls needed for retrieval/calculation).

        The plan is validated: field names / years / step references are checked
        against the sheet catalog. Fix any reported errors and call write_plan
        again until it is accepted.

        After acceptance, ALL retrieve and named-op steps are executed
        automatically. The results are returned to you in this tool response.
        You only need to call execute_python_code if a compute step requires
        custom code, then produce the final ExecutionResult.

        Args:
            plan: A structured QueryPlan with task_type, plan steps (for
                perform_calculations) or items (for retrieve_numbers), and
                description (for give_advice). Use exact field names and years
                from the sheet catalog to avoid rejection.
        """
        errors = validate_plan_semantics(plan, ctx.deps.sheet_metas)
        if errors:
            ctx.deps.plan_rejections += 1
            # Cap the revision loop: after 5 rejections, accept the plan as-is
            # so a confused agent can't burn unlimited model requests.
            if ctx.deps.plan_rejections >= 5:
                ctx.deps.plan = plan
                ctx.deps.emit("plan", {
                    "task_type": plan.task_type,
                    "plan": plan.model_dump().get("plan"),
                    "items": plan.items,
                    "description": plan.description,
                })
                print(f"⚠️ Plan accepted after {ctx.deps.plan_rejections} rejections "
                      f"(validation bypassed): {plan.task_type}")
                return (
                    f"Plan accepted with warnings after {ctx.deps.plan_rejections} "
                    "rejections (validation cap reached). Known issues:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nProceed with execution now. If a retrieval fails, note it "
                    "in friendly_response and continue with the data you have."
                )
            return (
                "PLAN REJECTED — fix these and call write_plan again:\n"
                + "\n".join(f"- {e}" for e in errors)
            )

        # Plan accepted — store and emit
        ctx.deps.plan = plan
        ctx.deps.emit("plan", {
            "task_type": plan.task_type,
            "plan": plan.model_dump().get("plan"),
            "items": plan.items,
            "description": plan.description,
        })
        print(f"📋 Plan accepted ({plan.task_type}, {n_steps_label(plan)}):",
              json.dumps(plan.model_dump(), indent=2, default=str))

        # ---- Deterministic execution (no model in the loop) ----
        # Execute all retrieve and named-op steps as plain Python calls.
        # Compute steps that need custom code are marked COMPUTE_NEEDED.
        from plan_executor import execute_plan, format_results_for_model
        step_results = await execute_plan(plan, ctx)
        results_text = format_results_for_model(step_results)
        print(f"⚡ Plan executed deterministically: {len(step_results)} steps")
        return results_text

    # Bridge plan/step events into the SSE queue via deps (tools emit through
    # ctx.deps.emit — see PipelineDeps.emit).

    return agent
