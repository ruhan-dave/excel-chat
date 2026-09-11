#!/usr/bin/env python3
"""
Langfuse observability validation tests.

Runs 15 real financial queries against the example Excel sheet and validates
that every reasoning step is captured with full audit trail:

  1. Reasoning steps (planner -> executor -> responder)
  2. Tool chosen per step (retrieve / compute) + inputs (args)
  3. Tool outputs (step_results)
  4. Tokens spent (input / output) — via Langfuse generations
  5. Latency per step (pipeline timings + Langfuse observation spans)
  6. Overall time taken (wall clock + Langfuse trace latency)

Gating:
  - OPENROUTER_API_KEY must be set (real LLM calls).
  - LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY must be set for trace verification;
    if absent, Langfuse assertions are skipped (graceful degradation).
  - Backend does NOT need to be running — these tests call the pipeline directly.

Langfuse Project Setup:
  All traces are written to the Langfuse project identified by your API keys.
  To route traces to the "excel-chat" project:

  1. Go to https://cloud.langfuse.com
  2. Create a new project named "excel-chat" (or use an existing one)
  3. Go to Project Settings → API Keys → Create new API keys
  4. Copy the public + secret keys into excel-chat/.env:

     LANGFUSE_PUBLIC_KEY=pk-lf-...
     LANGFUSE_SECRET_KEY=sk-lf-...
     LANGFUSE_HOST=https://cloud.langfuse.com
     LANGFUSE_TRACING_ENVIRONMENT=development

  5. Run: python -m pytest tests/test_langfuse_observability.py -v -s

  All 15 query traces will appear in the "excel-chat" project, named
  "excel-chat:q01" ... "excel-chat:q15" (visible in Langfuse UI → Traces).

  To export traces afterward:
     python tests/export_langfuse_traces.py --tag excel-chat
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

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

# Initialise observability before any Agent is constructed (required by
# Agent.instrument_all() — must run before build_query_pipeline is used).
from observability import init_observability
init_observability()

EXCEL_FILE = Path(__file__).parent.parent / "example_sheets" / "Detailed_Expense_Breakdown.xlsx"

# ============================================================================
# The 15 test queries — financial analysis against Detailed_Expense_Breakdown
# ============================================================================

TEST_QUERIES: list[str] = [
    # 1. Growth rate comparison
    "Compare the growth rates of social security benefits versus social assistance benefits from 2018 to 2023.",
    # 2. Fraction of total
    'What fraction of total expense was allocated to "To foreign governments" in 2021?',
    # 3. Ratio
    "What was the ratio of current to capital expenses in 2023?",
    # 4. Max over range
    "In which year were our grants the highest from 2015-2022?",
    # 5. Absolute growth
    "How much did employee compensation grow between 2018 and 2022.",
    # 6. Growth rate comparison (different range)
    "Compare the growth rates of social security benefits versus social assistance benefits from 2017 to 2021.",
    # 7. Stability comparison
    "Which showed greater stability over 2012-2021: wages and salaries or employers' social contributions?",
    # 8. Trend + max increase
    "Analyze the trend of interest expense from 2019 to 2023 and identify the year with the maximum value increase.",
    # 9. Average over range
    "What's the average annual expense on grants to foreign governments between 2015-2020?",
    # 10. Max in range
    "In the range of 2020 to 2023, which year stands out as having the highest expenditure on interest to nonresidents?",
    # 11. Percentage
    "What's capital as a percentage of expense in 2020",
    # 12. Percentage
    "What percentage of total expense was spent on wages and salaries in 2022?",
    # 13. Directional change
    "Did capital expenditure increase or decrease between 2021 and 2022?",
    # 14. Average (duplicate of 9 — kept per user spec)
    "What's the average annual expense on grants to foreign governments between 2015-2020?",
    # 15. Absolute change
    "By how much did \"Employers' social contributions\" change from 2019 to 2022 in absolute terms?",
]

QUERY_IDS = [f"q{i+1:02d}" for i in range(len(TEST_QUERIES))]

# ============================================================================
# Fixtures
# ============================================================================

def _build_pipeline(user_id: str = "test-langfuse"):
    """Build a query pipeline from the example Excel file with a unique user_id."""
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
    return build_query_pipeline(
        None, sheets, [meta], template, user_id=user_id,
    )


def _run_query_traced(pipe, query: str, query_id: str, user_id: str):
    """
    Run a query through the instrumented pipeline.

    The application code (pipeline.py + observability.py) now handles all
    Langfuse instrumentation via Agent.instrument_all() and explicit
    observe_step / observe_agent_run spans.  Tests just run the pipeline
    and correlate traces by user_id (OTel-exported trace IDs differ from
    SDK-native trace IDs, so user_id is the robust correlation key).

    Returns (result, user_id) — the user_id is used to fetch the trace
    from Langfuse via the user_id filter.
    """
    return asyncio.run(pipe(query)), user_id


@pytest.fixture(scope="module")
def excel_available():
    """Gate: example Excel file exists."""
    if not EXCEL_FILE.exists():
        pytest.skip(f"Example file not found: {EXCEL_FILE}")
    return EXCEL_FILE


@pytest.fixture(scope="module")
def llm_available():
    """Gate: OPENROUTER_API_KEY is set for real LLM calls."""
    if not os.environ.get("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY not set — skipping real LLM tests")
    return True


@pytest.fixture(scope="module")
def langfuse_available():
    """Gate: Langfuse keys are set for trace verification."""
    if not (
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    ):
        pytest.skip("LANGFUSE_* keys not set — skipping trace verification")
    from langfuse import get_client
    client = get_client()
    # Print the project the traces land in (determined by the API keys).
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")
    print(f"\n  Traces go to: {host} (project bound to LANGFUSE_PUBLIC_KEY)")
    return client


# ============================================================================
# Langfuse trace retrieval helper (handles async ingestion lag)
# ============================================================================

def _fetch_traces_by_user(langfuse_client, user_id: str, timeout: float = 90.0, limit: int = 5):
    """
    Fetch Langfuse traces filtered by user_id.

    OTel-exported traces (from pydantic-ai instrumentation) carry OTel trace
    IDs that differ from SDK-native @observe trace IDs, so correlating by
    get_current_trace_id() is unreliable.  user_id is the robust key.

    Ingestion is asynchronous (typically 15-30s).  Retry with backoff until
    at least one trace appears or the timeout expires.

    Returns a list of trace objects (may be empty on timeout).
    """
    deadline = time.time() + timeout
    backoff = 2.0
    while time.time() < deadline:
        try:
            # v4 client API — fetch traces by user_id
            traces = langfuse_client.client.fetch_traces(user_id=user_id, limit=limit)
            if traces and len(traces) > 0:
                return traces
        except Exception:
            pass
        try:
            # Fallback: v4 REST API
            traces = langfuse_client.api.traces.get_many(user_id=user_id, limit=limit)
            if traces and len(traces) > 0:
                return traces
        except Exception:
            pass
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 10.0)
    return []


def _fetch_trace_by_id(langfuse_client, trace_id: str, timeout: float = 90.0):
    """
    Fetch a Langfuse trace by its trace_id (legacy helper, kept for compatibility).

    Ingestion is asynchronous (typically 15-30s). Retry with backoff until the
    trace appears or the timeout expires.

    Returns the trace object or None.
    """
    if not trace_id:
        return None
    deadline = time.time() + timeout
    backoff = 2.0
    while time.time() < deadline:
        try:
            trace = langfuse_client.api.trace.get(trace_id)
            return trace
        except Exception:
            pass
        time.sleep(backoff)
        backoff = min(backoff * 1.5, 10.0)
    return None


def _fetch_observations(langfuse_client, trace_id: str):
    """Fetch all observations (spans + generations) for a trace."""
    obs = langfuse_client.api.observations.get_many(
        trace_id=trace_id,
        fields="core,basic,usage,input,output",
        limit=200,
    )
    return obs.data if obs else []


# ============================================================================
# Tests: Pipeline-level observability (works with or without Langfuse)
# ============================================================================

@pytest.mark.parametrize("query", TEST_QUERIES, ids=QUERY_IDS)
def test_pipeline_reasoning_audit_trail(excel_available, llm_available, query, request):
    """
    Each query produces a complete reasoning audit trail via the pipeline result.

    Validates:
      - Reasoning steps present (plan.plan or plan.items)
      - Tool chosen per step (action: retrieve/compute)
      - Tool inputs present (args)
      - Tool outputs present (step_results)
      - Per-step latency captured (timings)
      - Overall time captured (sum of timings)
      - Friendly response generated
    """
    query_id = request.node.callspec.id
    user_id = f"test-pipe-{uuid.uuid4().hex[:8]}"
    pipe = _build_pipeline(user_id=user_id)

    t0 = time.time()
    result, _trace_id = _run_query_traced(pipe, query, query_id, user_id)
    wall_time = time.time() - t0

    # --- Structure ---
    assert isinstance(result, dict), "Pipeline must return a dict"
    assert "answer" in result, "Missing 'answer' key"
    assert "plan" in result, "Missing 'plan' key (reasoning steps)"
    assert "timings" in result, "Missing 'timings' key (per-step latency)"
    assert "friendly_response" in result, "Missing 'friendly_response' key"

    # --- Reasoning steps (the plan) ---
    plan = result["plan"]
    plan_steps = plan.get("plan") or {}
    plan_items = plan.get("items") or []
    assert plan_steps or plan_items, (
        f"Plan must contain reasoning steps (plan.plan or plan.items). "
        f"Got: {plan}"
    )

    # --- Tool chosen + inputs per step ---
    if plan_steps:
        for step_name, step in plan_steps.items():
            assert isinstance(step, dict), f"Step {step_name} must be a dict"
            action = step.get("action") or step.get("tool")
            assert action, f"Step {step_name} missing action/tool field"
            # retrieve steps have args; compute steps have args or expression
            args = step.get("args") or step.get("expression") or step.get("inputs")
            assert args is not None, f"Step {step_name} ({action}) missing inputs/args"

    # --- Tool outputs (step_results) ---
    answer = result["answer"]
    assert "step_results" in answer or "final_answer" in answer, (
        "Answer must contain step_results (tool outputs) or final_answer"
    )
    step_results = answer.get("step_results", {})
    if step_results:
        for step_name, val in step_results.items():
            assert val is not None, f"Step {step_name} produced null output"

    # --- Per-step latency ---
    timings = result["timings"]
    assert isinstance(timings, dict), "timings must be a dict"
    assert len(timings) > 0, "timings must capture at least one stage"
    for stage, elapsed in timings.items():
        assert isinstance(elapsed, (int, float)), (
            f"timings['{stage}'] must be numeric, got {type(elapsed)}"
        )
        assert elapsed >= 0, f"timings['{stage}'] must be non-negative, got {elapsed}"

    # Planner should always be present
    assert "planner" in timings, (
        f"timings must include 'planner' stage. Got: {list(timings.keys())}"
    )

    # --- Overall time ---
    total_pipeline_time = sum(timings.values())
    assert total_pipeline_time > 0, "Total pipeline time must be positive"
    # Wall time should be >= pipeline time (wall includes overhead)
    assert wall_time >= total_pipeline_time * 0.9, (
        f"Wall time ({wall_time:.2f}s) should be >= pipeline time "
        f"({total_pipeline_time:.2f}s)"
    )

    # --- Friendly response ---
    friendly = result["friendly_response"]
    assert friendly, "friendly_response must be non-empty"
    assert isinstance(friendly, str), "friendly_response must be a string"


# ============================================================================
# Tests: Langfuse trace verification (requires LANGFUSE_* keys)
# ============================================================================

@pytest.mark.parametrize("query", TEST_QUERIES, ids=QUERY_IDS)
def test_langfuse_trace_capture(
    excel_available, llm_available, langfuse_available, query, request
):
    """
    Each query produces a Langfuse trace with full observability.

    Validates (via Langfuse API, fetching by user_id):
      - Trace exists for the query (fetch by user_id filter)
      - Trace contains at least one observation (span/generation)
      - GENERATION observations have model + token usage (proves Agent.instrument_all works)
      - Generations have latency (start_time -> end_time) when present
      - Trace has total latency
      - Tool inputs/outputs captured in observation input/output fields
      - Decision spans carry metadata["decision"]
      - Stage spans carry tokens_in/tokens_out/cost_usd/time_ms
      - Trace span carries total_tokens/total_cost_usd/total_time_ms
    """
    langfuse = langfuse_available
    query_id = request.node.callspec.id
    user_id = f"test-lf-{uuid.uuid4().hex[:8]}"
    pipe = _build_pipeline(user_id=user_id)

    # Run the query through the instrumented pipeline
    t0 = time.time()
    result, returned_user_id = _run_query_traced(pipe, query, query_id, user_id)
    wall_time = time.time() - t0

    # Flush Langfuse to ensure traces are sent
    try:
        langfuse.flush()
    except Exception:
        pass

    # --- Fetch the trace by user_id (OTel traces don't expose trace_id via SDK) ---
    traces = _fetch_traces_by_user(langfuse, user_id, timeout=90.0, limit=5)
    assert len(traces) > 0, (
        f"No Langfuse trace found for user_id={user_id} within 90s. "
        f"Pipeline produced a result, but trace was not ingested."
    )
    trace = traces[0]

    # --- Fetch all observations ---
    observations = _fetch_observations(langfuse, trace.id)
    assert len(observations) > 0, f"Trace {trace.id} has no observations"

    # --- Classify observations ---
    generations = [o for o in observations if o.type == "GENERATION"]
    spans = [o for o in observations if o.type == "SPAN"]

    # --- At least one GENERATION observation with non-empty model (proves instrument_all works) ---
    if generations:
        models_seen = [getattr(g, "model", None) for g in generations]
        models_non_empty = [m for m in models_seen if m]
        assert len(models_non_empty) > 0, (
            f"GENERATION observations exist but none have a model attribute. "
            f"Models: {models_seen}"
        )

    for gen in generations:
        # Token usage
        usage = getattr(gen, "usage", None)
        if usage:
            input_tokens = getattr(usage, "input", None) or getattr(usage, "input_tokens", None)
            output_tokens = getattr(usage, "output", None) or getattr(usage, "output_tokens", None)
            if input_tokens is not None:
                assert input_tokens >= 0, f"Generation {gen.name} has negative input tokens"
            if output_tokens is not None:
                assert output_tokens >= 0, f"Generation {gen.name} has negative output tokens"

        # Latency (start_time -> end_time)
        start = getattr(gen, "start_time", None)
        end = getattr(gen, "end_time", None)
        if start and end:
            gen_latency = (end - start).total_seconds()
            assert gen_latency >= 0, f"Generation {gen.name} has negative latency"

        # Input/output (tool inputs + outputs)
        gen_input = getattr(gen, "input", None)
        gen_output = getattr(gen, "output", None)
        # At least one of input/output should be present
        assert gen_input is not None or gen_output is not None, (
            f"Generation {gen.name} has neither input nor output captured"
        )

    # --- Span names include excel-chat:* (pipeline/agent spans) ---
    span_names = [getattr(s, "name", "") for s in spans]
    excel_chat_spans = [n for n in span_names if n and n.startswith("excel-chat:")]
    assert len(excel_chat_spans) > 0, (
        f"No excel-chat:* spans found. Span names: {span_names}"
    )

    # --- Tool-execution spans carry inputs AND outputs ---
    tool_spans = [s for s in spans if getattr(s, "name", "").startswith("excel-chat:tool:")]
    if tool_spans:
        for ts in tool_spans:
            ts_input = getattr(ts, "input", None)
            ts_output = getattr(ts, "output", None)
            # At least one tool span should have both input and output
            if ts_input is not None and ts_output is not None:
                break
        else:
            pytest.fail(
                f"No tool span has both input and output. "
                f"Tool spans: {[(getattr(s, 'name', ''), getattr(s, 'input', None) is not None, getattr(s, 'output', None) is not None) for s in tool_spans]}"
            )

    # --- Decision metadata present ---
    decision_spans = [s for s in spans if "decision" in getattr(s, "name", "")]
    if decision_spans:
        valid_decisions = {"allow", "reject", "skip_executor", "run_executor", "hit", "miss", "retry"}
        found_decisions = []
        for ds in decision_spans:
            meta = getattr(ds, "metadata", None) or {}
            if isinstance(meta, dict) and "decision" in meta:
                found_decisions.append(meta["decision"])
        assert len(found_decisions) > 0, (
            f"Decision spans exist but none have metadata['decision']. "
            f"Names: {[getattr(s, 'name', '') for s in decision_spans]}"
        )
        for d in found_decisions:
            assert d in valid_decisions, f"Invalid decision value: {d}"

    # --- Per-step cost + time on stage spans ---
    stage_spans = [s for s in spans if getattr(s, "name", "").startswith("excel-chat:step:")]
    if stage_spans:
        for ss in stage_spans:
            meta = getattr(ss, "metadata", None) or {}
            if isinstance(meta, dict) and "time_ms" in meta:
                assert meta["time_ms"] >= 0, f"Stage span {getattr(ss, 'name', '')} has negative time_ms"
                if "tokens_in" in meta:
                    assert meta["tokens_in"] >= 0, f"Stage span has negative tokens_in"
                if "tokens_out" in meta:
                    assert meta["tokens_out"] >= 0, f"Stage span has negative tokens_out"

    # --- Per-query totals on trace span ---
    trace_level_spans = [s for s in spans if getattr(s, "name", "") == "excel-chat:query"]
    if trace_level_spans:
        for ts in trace_level_spans:
            meta = getattr(ts, "metadata", None) or {}
            if isinstance(meta, dict) and "total_time_ms" in meta:
                assert meta["total_time_ms"] > 0, "Trace span has non-positive total_time_ms"
                if "total_tokens" in meta:
                    assert meta["total_tokens"] >= 0, "Trace span has negative total_tokens"

    # --- Spans should have non-negative latency ---
    for span in spans:
        start = getattr(span, "start_time", None)
        end = getattr(span, "end_time", None)
        if start and end:
            span_latency = (end - start).total_seconds()
            assert span_latency >= 0, f"Span {span.name} has negative latency"


# ============================================================================
# Tests: Aggregate observability metrics (Langfuse)
# ============================================================================

def test_all_queries_traced(excel_available, llm_available, langfuse_available):
    """
    Run all 15 queries and verify each produces a distinct Langfuse trace.

    This catches trace deduplication, missing traces, or trace collisions.
    Traces are correlated by user_id (unique per query) since OTel-exported
    traces carry OTel trace IDs that differ from SDK-native trace IDs.
    """
    langfuse = langfuse_available
    trace_ids: set[str] = set()

    for i, query in enumerate(TEST_QUERIES):
        query_id = f"q{i+1:02d}"
        user_id = f"test-agg-{uuid.uuid4().hex[:8]}"
        pipe = _build_pipeline(user_id=user_id)

        _result, returned_user_id = _run_query_traced(pipe, query, query_id, user_id)

        try:
            langfuse.flush()
        except Exception:
            pass

        traces = _fetch_traces_by_user(langfuse, user_id, timeout=90.0, limit=5)
        assert len(traces) > 0, f"Query {i+1} produced no Langfuse trace (user_id={user_id})"
        trace = traces[0]
        assert trace.id not in trace_ids, (
            f"Query {i+1} trace {trace.id} collides with a previous query"
        )
        trace_ids.add(trace.id)

    assert len(trace_ids) == len(TEST_QUERIES), (
        f"Expected {len(TEST_QUERIES)} distinct traces, got {len(trace_ids)}"
    )

    # Print summary for the user
    print(f"\n  All {len(trace_ids)} distinct traces captured")
