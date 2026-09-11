# Feature: Langfuse Observability Layer

The following plan should be complete, but validate documentation and codebase patterns and task sanity before you start implementing.

Pay special attention to naming of existing utils types and models. Import from the right files etc.

## Feature Description

Instrument the RagSheets backend with a real Langfuse observability layer so every query produces a complete, auditable trace **in the "excel-chat" Langfuse project** (routing is determined by project-scoped API keys). Every step of the pipeline writes its **decision, tool call, tool-call inputs, and tool-call outputs** to the trace:

- One trace per user query (`/query` and `/query/stream`), tagged with `user_id`, `session_id`, and query metadata — plus a request span for **every** API endpoint (via FastAPI middleware) so uploads, describe-sheet, thread, and file operations are traced too.
- **All traces routed to the "excel-chat" Langfuse project** by creating a project-scoped API key pair (`LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` from that project's settings) and setting them in `.env` + Railway.
- Nested spans for each pipeline stage: planner, pre_populate, executor, and guardrail screening + cache lookups — each recording `input` (arguments/prompt, truncated) and `output` (result/decision).
- **Tool calls captured with inputs and outputs**: pydantic-ai OTel instrumentation records agent tool executions (retrieve_values, execute_python_code, EDA tools) automatically; the pure-Python `_prepopulate_retrievals` path gets explicit spans since it bypasses the agent tool dispatcher.
- **Decisions captured as span metadata**: guardrail allow/reject (+reason), semantic cache hit/miss (+similarity score), cache write success/failure, fallback attempt chosen (primary/fallback/retry + model), and pipeline short-circuit choices (pure-retrieve vs executor).
- **Token cost + time per reasoning step**: each pipeline stage span (planner, pre_populate, executor, responder) records `tokens_in`, `tokens_out`, `cost_usd`, and `time_ms` — extracted from pydantic-ai's `AgentRunResult.usage` (which provides `input_tokens`, `output_tokens`, and `cost` via genai-prices) and the existing `timed()` dict. These appear alongside the decision/tool/input/output metadata on the same span.
- **Total time + tokens per query**: the trace-level span (`excel-chat:query`) records `total_tokens_in`, `total_tokens_out`, `total_tokens`, `total_cost_usd`, and `total_time_ms` — aggregated across all stages. Visible in the Langfuse trace header alongside the reasoning tree.
- Automatic LLM generations (per model call, tokens, latency) via Pydantic AI's native OpenTelemetry instrumentation (`Agent.instrument_all()` + `instrument=True`).
- Graceful degradation: if `LANGFUSE_*` keys are missing, the app runs exactly as today (no crashes, no network calls, ~zero overhead).
- PII masking helper for production traces (email/phone redaction).
- Unit tests that never emit traces + integration tests (already scaffolded in `tests/test_langfuse_observability.py`) that verify real traces land in the "excel-chat" Langfuse project.

The Langfuse dependency (`langfuse>=4.0.0,<5`) is already pinned in `backend/requirements.txt:137`; the observability test scaffolding exists untracked (`tests/test_langfuse_observability.py`, `tests/export_langfuse_traces.py`). What's missing is the actual application-level instrumentation.

## User Story

As a developer maintaining RagSheets,
I want every query to be automatically traced with per-stage spans, LLM token usage, and user context in Langfuse,
So that I can debug failures, measure latency/cost per pipeline stage, and validate the agent's reasoning without adding manual logging.

## Problem Statement

1. **No app-level tracing.** `pipeline.py` and `main.py` contain zero Langfuse references. The only observability is console `print()` statements and the per-stage `timings` dict returned in the response payload (`pipeline.py:48-67`, `main.py:641`).
2. **The existing observability test cheats.** `tests/test_langfuse_observability.py` wraps the pipeline in `@observe()` *inside the test* (`_run_query_traced`, lines 141-169), so traces only appear when the test injects them — not in production. Its comments even note "once the pipeline agents are instrumented with instrument=True" (line 397).
3. **No graceful-degradation story.** Nothing guards against missing `LANGFUSE_*` keys; once instrumentation is added naively it could crash the app when keys are absent.
4. **Validation doc expects an observability module.** `core_piv_loop/validate-observability.md` expects `src/shared/observability.py` exposing `observe_agent_run` and `mask_pii` (this repo is flat `backend/src/`, so the module goes at `backend/src/observability.py` and the validation doc's paths need adapting).
5. **No project routing or step-level audit trail.** Nothing pins traces to the "excel-chat" project (keys must come from that project's settings), and decisions/tool calls are only visible as console `print()`s — guardrail allow/reject, cache hit/miss + similarity, fallback attempts, short-circuit choices, and tool inputs/outputs are absent from any trace. Non-agent tool execution (`_prepopulate_retrievals` calls `retrieve_values` directly, `pipeline.py:620-674`) would be invisible to agent-level instrumentation.

## Solution Statement

Create a single `backend/src/observability.py` module that:

- Reads Langfuse config from env vars (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` — v4-preferred; `LANGFUSE_HOST` accepted as deprecated fallback; optional `LANGFUSE_TRACING_ENVIRONMENT`, `LANGFUSE_DEBUG`). **The keys MUST be generated from the "excel-chat" project's Settings → API Keys page** so every trace is routed to that project (a Langfuse project is identified by its key pair — see setup task below).
- Exposes `init_observability()` which calls `Agent.instrument_all()` **only when keys are present** (must run before any agent is built).
- Exposes `observe_agent_run(...)` (context manager wrapping `start_as_current_observation` + `propagate_attributes`) for trace-level attributes, plus `observe_step(...)` — a span helper that records **input on entry and output/error on exit** (with optional `update()` for incremental decision metadata) — `mask_pii()`, `is_observability_enabled()`, and `flush_observability()`.
- Degrades to no-ops when keys are missing — mirroring the existing "cache failure must NEVER break a query" philosophy (`main.py:532-535`).

Then wire it in:

- **Project setup (manual, one-time):** create/select the "excel-chat" project in Langfuse, create an API key pair in that project, set `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_BASE_URL` in `backend/src/.env` (and Railway env). This is what routes ALL traces to the "excel-chat" project.
- `pipeline.py`: `instrument=True` on the 3 agent constructors (`build_planner_agent` line 347, `build_executor_agent` line 401, `build_responder_agent` line 434) — agent tool calls (retrieve_values, execute_python_code, EDA tools) are then recorded automatically with inputs/outputs; wrap `run_pipeline` in an observed span with `propagate_attributes(user_id=..., tags=["excel-chat"], metadata={"task_type": ...})`; add `observe_step` spans for `_prepopulate_retrievals` (input = step args, output = retrieved values) and each short-circuit decision; add per-attempt spans inside `_run_with_fallback` (primary/fallback/retry, input = truncated prompt, output = result or error); **extract `result.usage` (input_tokens, output_tokens, cost) from each agent result and record on the stage span + aggregate into trace-level totals**; **record `time_ms` from the `timings` dict on each stage span and `total_time_ms` on the trace span**.
- `main.py`: FastAPI middleware span for **every** endpoint (method, path, status, user_id) + request-level spans for `query_rag` (470) and `query_stream` (660); decision spans for guardrail (allowed/reason) and semantic cache (hit/miss + similarity) at both call sites; `flush_observability()` in the lifespan shutdown hook (`_lifespan`, line 96).
- `tests/conftest.py` (new): autouse fixture that strips `LANGFUSE_*` keys for unit tests so CI never emits traces, unless `LANGFUSE_ENABLED_IN_TESTS=1`.
- Update `tests/test_langfuse_observability.py` to fetch traces by `user_id`/`session_id`/tag filters (OTel traces have different IDs than SDK `@observe` traces) and assert agent-instrumented generations **and tool-execution spans with inputs/outputs** exist.

## Feature Metadata

**Feature Type**: New Capability
**Estimated Complexity**: Medium
**Primary Systems Affected**: `backend/src/observability.py` (new), `backend/src/pipeline.py`, `backend/src/main.py`, `tests/test_langfuse_observability.py`, `tests/conftest.py` (new)
**Dependencies**: `langfuse>=4.0.0,<5` (already pinned, `backend/requirements.txt:137`), `pydantic-ai-slim==2.30.0` (already pinned, `backend/requirements.txt:136` — OTel instrumentation built-in), `opentelemetry-*` (already in requirements via langfuse deps)

---

## CONTEXT REFERENCES

### Relevant Codebase Files — READ THESE BEFORE IMPLEMENTING!

- `backend/src/pipeline.py` (lines 48-67) — Why: `timed()` context manager, the existing per-stage timing pattern we mirror for observability spans.
- `backend/src/pipeline.py` (lines 217-229) — Why: `build_openrouter_model` — single model factory; instrumentation must not break it.
- `backend/src/pipeline.py` (lines 236-353, 356-427, 430-446) — Why: the 3 agent builders; `Agent(...)` constructors at lines 347, 401, 434 get `instrument=True`.
- `backend/src/pipeline.py` (lines 485-537) — Why: `_run_with_fallback` — 3-attempt retry; add per-attempt spans here (input prompt, model, attempt label, output result or error).
- `backend/src/pipeline.py` (lines 620-674) — Why: `_prepopulate_retrievals` — calls `retrieve_values` DIRECTLY (not via agent tool dispatcher), so these tool calls + inputs + outputs need explicit `observe_step` spans or they'll be invisible to pydantic-ai instrumentation.
- `backend/src/pipeline.py` (lines 786-826, 834-955, 957-1047) — Why: short-circuit decision points (pure-retrieve, retrieve_numbers-items, executor path) — each must record its decision + input (plan) + output (execution/step_results) as span metadata.
- `backend/src/pipeline.py` (lines 712-1049) — Why: `build_query_pipeline` / `run_pipeline` — the per-query entry point to wrap with `observe_agent_run` + `propagate_attributes`.
- `backend/src/main.py` (lines 96-143) — Why: `_lifespan`, `app`, and the existing middleware/CORS block — add the all-endpoints request span middleware here + `flush_observability()` on shutdown.
- `backend/src/main.py` (lines 470-653) — Why: `query_rag` — request-level span + user attributes; guardrail call at 483; semantic cache block at 500-535 (hit/miss decision + similarity must be recorded).
- `backend/src/main.py` (lines 660-950) — Why: `query_stream` (SSE) — request-level span + user attributes; guardrail call at 696; semantic cache block at 702-764 (hit/miss decision + similarity must be recorded).
- `backend/src/guardrails.py` (lines 417-448) — Why: `screen_query` — rule-based (no LLM), do NOT instrument inside; wrap at call sites in main.py instead, recording the allow/reject decision + reason.
- `backend/requirements.txt` (line 137) — Why: `langfuse>=4.0.0,<5` already pinned — v4 = OTel-based SDK, do not downgrade.
- `tests/test_langfuse_observability.py` (lines 21-42) — Why: docstring already documents the "excel-chat" project setup (create project → Project Settings → API Keys → set `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_HOST` in `.env`). This is the project-routing source of truth.
- `tests/test_langfuse_observability.py` (lines 141-169) — Why: `_run_query_traced` currently does test-side `@observe`; must be refactored to rely on app instrumentation.
- `tests/test_langfuse_observability.py` (lines 208-239, 338-437, 444-477) — Why: trace-fetch helpers (`_fetch_trace_by_id` retry/backoff) + the two trace-verification tests to update with tool-span and decision-metadata assertions.
- `tests/export_langfuse_traces.py` (lines 116-178, 386-421) — Why: already fetches by user_id prefix/session/tag — reuse this pattern; set the default `--tag excel-chat` so exports always pull from the right project.
- `core_piv_loop/validate-observability.md` (lines 1-230) — Why: the acceptance checklist this feature must satisfy (env vars, connectivity, trace structure, cost tracking, graceful degradation, PII masking). NOTE: it references `src/shared/observability.py` — adapt to `backend/src/observability.py`.
- `plans/agent-architecture.md` (section 13, "Timing & Observability") — Why: canonical design doc; update to reflect the Langfuse layer.

### New Files to Create

- `backend/src/observability.py` — Core observability module (client init, `init_observability`, `observe_agent_run`, `observe_step`, `mask_pii`, `flush_observability`, graceful degradation).
- `tests/conftest.py` — Autouse fixture disabling Langfuse in unit tests (per validate-observability.md step 8).
- `tests/test_observability_unit.py` — Offline unit tests (no network, no keys): PII masking, graceful degradation, env parsing.

### MCP Servers Required

None. (This is a backend tracing feature; no MCP involvement.)

### Relevant Documentation — READ BEFORE IMPLEMENTING!

- [Langfuse + Pydantic AI integration](https://langfuse.com/integrations/frameworks/pydantic-ai.md)
  - Specific section: Steps 2-4 (SDK config via `get_client()`, `Agent.instrument_all()`, `instrument=True` on Agent)
  - Why: The exact v4 integration pattern — this repo is pinned to langfuse v4.
- [Langfuse SDK instrumentation (custom spans, observe, propagate_attributes)](https://langfuse.com/docs/observability/sdk/instrumentation.md)
  - Specific section: "Observe wrapper", "Manual observations", "Nesting observations"
  - Why: `observe_agent_run` and per-stage spans are built on these APIs.
- [Langfuse Python v3 → v4 upgrade path](https://langfuse.com/docs/observability/sdk/upgrade-path/python-v3-to-v4.md)
  - Specific section: env var renames (`LANGFUSE_HOST` → `LANGFUSE_BASE_URL`, backward compatible)
  - Why: avoid using removed v3 APIs (`langfuse.pydantic_ai.Langfuse` wrapper no longer the pattern).
- [Pydantic AI Instrumentation capability](https://pydantic.dev/docs/ai/capabilities/instrumentation/)
  - Specific section: `Agent.instrument_all()` vs per-agent `capabilities=[Instrumentation(settings=...)]`
  - Why: `instrument=True` is deprecated in newer pydantic-ai; verify against installed 2.30.0 and prefer the capability API if `instrument=True` warns.

### Patterns to Follow

**Naming Conventions:**
- snake_case modules/functions, matching `timed()`, `build_query_pipeline`, `_run_with_fallback`, `screen_query`.
- Private helpers prefixed `_` (e.g. `_get_client`, `_is_enabled`).
- Span names snake_case with `excel-chat:` prefix to match the existing trace naming in tests (`excel-chat:q01`, see `tests/test_langfuse_observability.py:161`).
- Decision spans use the suffix `:decision` (e.g. `excel-chat:decision:guardrail`, `excel-chat:decision:semantic_cache`, `excel-chat:decision:short_circuit`) and store `{"decision": ..., "input": ..., "output": ...}` in metadata. Tool/step spans use `excel-chat:tool:<name>` / `excel-chat:step:<name>` with `input`/`output` fields.

**Error Handling:**
- NEVER let observability break the query. Wrap all langfuse calls in try/except, print `⚠️` warning on failure (mirror `main.py:532-535` semantic-cache comment: "must NEVER break a query — degrade").
- Guard `init_observability()` on key presence; no keys → all helpers no-op.

**Logging Pattern:**
- Existing pattern: `print(f"⚠️ ...")` for recoverable failures, `print(f"✅ ...")` for success. Follow it in `observability.py`.
- Enable `LANGFUSE_DEBUG=True` support for troubleshooting (official doc troubleshooting section).

**Other Relevant Patterns:**
- Env config: `os.environ.get("VAR", default)` like `PRIMARY_MODEL` (`pipeline.py:213-214`); `.env` loaded by callers (tests use `load_dotenv()`, see `tests/test_langfuse_observability.py:60-62`).
- Test gating: module-scoped pytest fixtures that `pytest.skip(...)` when keys are missing (`llm_available`, `langfuse_available` at `tests/test_langfuse_observability.py:180-201`).
- Module layout: flat files in `backend/src/`, imported directly (`from sheet_metadata import SheetMeta`).
- Input/output capture: spans record `input` (args/prompt, truncated to ~500 chars, PII-masked) at entry and `output` (result or decision) at exit — same shape the export script already reads (`tests/export_langfuse_traces.py:58-113` handles dict-style input/output).

---

## IMPLEMENTATION PLAN

### Phase 1: Foundation

**Tasks:**
- **Set up the "excel-chat" Langfuse project + API keys** (manual, one-time): create/select the project in Langfuse, generate a project-scoped key pair, and record them in `backend/src/.env` — this is what routes ALL traces to the "excel-chat" project.
- Create `backend/src/observability.py` with env parsing, client init, graceful degradation, and helper API.
- Verify the installed `langfuse` + `pydantic_ai` versions and their exact APIs (introspect signatures before wiring).

### Phase 2: Core Implementation

**Tasks:**
- Implement `init_observability()` (guarded `Agent.instrument_all()`).
- Implement `observe_agent_run(...)` context manager (trace-level span + `propagate_attributes`) and `observe_step(...)` span helper (records `input` on entry, `output`/`error` on exit, supports `span.update()` for incremental decision metadata).
- Implement `mask_pii()`, `is_observability_enabled()`, `flush_observability()`.

### Phase 3: Integration

**Tasks:**
- Add `instrument=True` (or capability equivalent) to the 3 agents in `pipeline.py` (auto-captures agent tool calls + inputs/outputs).
- Wrap `run_pipeline` and `_run_with_fallback` attempts with spans/attributes.
- Add `observe_step` spans for `_prepopulate_retrievals` (tool input args → output values) and decision spans for the three short-circuit paths.
- **Extract `result.usage` (tokens + cost) from each agent result and record on stage spans; aggregate `total_tokens`/`total_cost_usd`/`total_time_ms` on the trace span.**
- Add all-endpoint request middleware + request-level spans for `query_rag`/`query_stream`, guardrail decision spans, semantic-cache hit/miss decision spans, and shutdown flush in `main.py`.
- Update `tests/test_langfuse_observability.py`; add `tests/conftest.py` + `tests/test_observability_unit.py`.

### Phase 4: Testing & Validation

**Tasks:**
- Offline unit tests pass without any keys (graceful degradation).
- Integration test with real keys produces verifiable traces in the **"excel-chat" project** (per validate-observability.md).
- Manual verification: run a query through `/query/stream`, confirm in the Langfuse UI that the trace tree shows decisions (guardrail, cache, short-circuit), tool calls with inputs/outputs, and LLM generations.

---

## STEP-BY-STEP TASKS

Execute every task in order, top to bottom. Each task is atomic and independently testable.

### SETUP Langfuse project "excel-chat" + project-scoped API keys (manual, one-time)

- **IMPLEMENT**: A Langfuse *project* is identified by its API key pair — traces land in whichever project the keys belong to. To route everything to "excel-chat":
  1. Open https://cloud.langfuse.com (or the `LANGFUSE_BASE_URL` host) → create a project named **excel-chat** (or select the existing one).
  2. Project Settings → API Keys → Create new API keys.
  3. Copy the `pk-lf-...` and `sk-lf-...` pair into `backend/src/.env` (which is gitignored — see `backend/src/.gitignore`):
     ```
     LANGFUSE_PUBLIC_KEY=pk-lf-...
     LANGFUSE_SECRET_KEY=sk-lf-...
     LANGFUSE_BASE_URL=https://cloud.langfuse.com
     LANGFUSE_TRACING_ENVIRONMENT=development
     ```
  4. Add the same vars to Railway env (dashboard or `railway variables`) for deployed traces.
- **PATTERN**: This is already documented in the test docstring at `tests/test_langfuse_observability.py:21-42` — follow it.
- **GOTCHA**: Never commit real keys — `backend/src/.env` is gitignored; only document placeholder values in README/architecture docs.
- **GOTCHA**: `LANGFUSE_HOST` still works (deprecated) but prefer `LANGFUSE_BASE_URL` (v4, langfuse-python PR #1418); the SDK supports both — set only one.
- **VALIDATE**: `python -c "from langfuse import get_client; c = get_client(); print(c.auth_check())"` run from `backend/src` with the `.env` loaded → prints `True` and the project visible at cloud.langfuse.com/project/... is "excel-chat".

### CREATE backend/src/observability.py

- **IMPLEMENT**: Module with the following public API:
  - `LANGFUSE_ENV_VARS = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"]`
  - `is_observability_enabled() -> bool` — True iff `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are both non-empty.
  - `get_langfuse()` — returns the cached client from `langfuse.get_client()` (v4), or `None` if disabled. Wrap init in try/except; on failure set an internal `_disabled_reason` and return `None` (never raise).
  - `init_observability() -> bool` — if disabled, print `⚠️ Langfuse observability disabled (LANGFUSE_* keys missing)` and return False. If enabled: create the client, then call `from pydantic_ai.agent import Agent; Agent.instrument_all()`. Must be idempotent (guard with a module flag) and MUST run before any `Agent(...)` is constructed.
  - `observe_agent_run(name: str, user_id: str = "anonymous", session_id: str | None = None, tags: list[str] | None = None, metadata: dict | None = None)` — context manager. If disabled: `@contextmanager` no-op yielding `None`. If enabled: `with get_langfuse().start_as_current_observation(as_type="span", name=name) as span:` then `propagate_attributes(user_id=..., session_id=..., tags=..., metadata=...)` (import from `langfuse`). Wrap body in try/except so span.end() always fires.
  - `observe_step(name: str, input: Any = None, metadata: dict | None = None)` — context manager yielding a lightweight proxy with:
    - on entry: `span.update(input=_truncate(mask_pii(input)))` (input = tool args / prompt / decision inputs, truncated to ~500 chars, PII-masked in production);
    - on success exit: `span.update(output=...)` via `proxy.set_output(output)` (tool result / decision output) before exiting the `with` block;
    - on exception: `span.update(metadata={**(metadata or {}), "error": f"{type(exc).__name__}: {exc}"})` then re-raise (decisions and failures must never be lost);
    - `proxy.record(decision=..., output=..., **extra)` helper for incrementally attaching decision metadata mid-span (e.g. guardrail reason, cache similarity).
    - `proxy.record_usage(usage: RunUsage, time_ms: float)` — extracts `input_tokens`, `output_tokens`, `cost` from a pydantic-ai `RunUsage` object and attaches them to the span metadata as `{"tokens_in": ..., "tokens_out": ..., "total_tokens": ..., "cost_usd": ..., "time_ms": ...}`. Handles `cost=None` (unknown pricing) gracefully by recording `cost_usd: null`. This makes token cost + time visible on every reasoning step span alongside its decision/tool/input/output data.
    - `proxy.record_totals(tokens_in: int, tokens_out: int, cost_usd: float | None, time_ms: float)` — attaches `{"total_tokens_in": ..., "total_tokens_out": ..., "total_tokens": ..., "total_cost_usd": ..., "total_time_ms": ...}` to the span. Used on the trace-level span to surface per-query aggregates.
    If disabled: `@contextmanager` no-op yielding `None` (call sites can `if proxy: proxy.record(...)`).
  - `mask_pii(text: str) -> str` — regex-replace emails → `[EMAIL]` and phone numbers (US 10-digit with separators) → `[PHONE]`. Return text unchanged when `LANGFUSE_TRACING_ENVIRONMENT == "production"` is False (i.e., only mask in production, per validate-observability.md section 6).
  - `flush_observability() -> None` — call `get_langfuse().flush()` inside try/except (short-lived apps / FastAPI shutdown; see official doc troubleshooting: "Call langfuse.flush() at the end of your application").
- **PATTERN**: Env parsing style from `pipeline.py:213-214`; graceful degradation from `main.py:532-535`; `@contextmanager` pattern from `pipeline.py:48-67`.
- **IMPORTS**: `os`, `re`, `json`, `contextlib.contextmanager`, `typing`, `langfuse` (lazy import inside functions so module import never fails when langfuse is missing).
- **GOTCHA**: v4 SDK reads `LANGFUSE_BASE_URL`; `LANGFUSE_HOST` still works but is deprecated (langfuse-python PR #1418). Support both: `os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")`. Do NOT use the removed v3 wrapper `langfuse.pydantic_ai.Langfuse`.
- **GOTCHA**: `Agent.instrument_all()` must not be called when keys are missing — with no exporter configured it is a no-op, but calling it unconditionally also means OTel spans are created for nothing; gate it on `is_observability_enabled()`.
- **GOTCHA**: `observe_step` must be safe to nest inside `observe_agent_run` (SDK spans nest via context propagation — see the Langfuse instrumentation doc "Nesting observations").
- **VALIDATE**: `python -m py_compile backend/src/observability.py`

### VERIFY backend/src/observability.py API against installed SDK

- **IMPLEMENT**: Run introspection to confirm the exact v4 API surface before wiring:
  - `python -c "import langfuse; print(langfuse.__version__)"` — expect 4.x (requirements pin `>=4.0.0,<5`).
  - `python -c "from langfuse import get_client, observe, propagate_attributes; print('ok')"` — confirm v4 exports.
  - `python -c "from pydantic_ai.agent import Agent; print(hasattr(Agent, 'instrument_all'))"` — expect True.
  - Check `inspect.signature(Agent.__init__)` for `instrument` param; if it warns as deprecated, use `from pydantic_ai.capabilities import Instrumentation, InstrumentationSettings` + `capabilities=[Instrumentation(settings=InstrumentationSettings(include_binary_content=False))]` per the pydantic-ai instrumentation doc instead of `instrument=True`.
- **GOTCHA**: pydantic-ai 2.30.0 may print deprecation warnings for `instrument=True` (still functional). Prefer the warning-free path that the installed version supports; the Langfuse official doc's `instrument=True` remains the documented default.
- **VALIDATE**: All four introspections print successfully in the app's venv (run from `backend/` where `uv`/venv has the pinned deps).

### UPDATE backend/src/pipeline.py — instrument the 3 agents

- **IMPLEMENT**: In `build_planner_agent` (Agent constructor at line 347), `build_executor_agent` (line 401), `build_responder_agent` (line 434): add `instrument=True` (or the capability equivalent verified in the previous task) to each `Agent(...)` call.
- **PATTERN**: Keep the `build_agent(*args, **kw)` indirection in `_run_with_fallback` (line 517) intact — agents are rebuilt per attempt, which is fine: instrumentation is a constructor-level flag.
- **GOTCHA**: Do NOT add `instrument=` to `build_openrouter_model` (line 217) — it returns a model, not an agent.
- **VALIDATE**: `python -m py_compile backend/src/pipeline.py`

### ADD backend/src/pipeline.py — trace metadata, tool-call spans, decision spans, per-step + per-query cost/time

- **IMPLEMENT**: In `run_pipeline` (starts line 737), wrap the entire body with:
  ```python
  with observe_agent_run(
      name="excel-chat:query",
      user_id=user_id,
      tags=["excel-chat", "query", plan.task_type],
      metadata={"sheets": len(sheets), "files": len({m.file_id for m in sheet_metas})},
  ) as trace_span:
  ```
  (Use `plan` where available; safe defaults before plan exists.) Keep the existing `timed()` calls and `print` statements untouched — observability augments, never replaces them. Record the final pipeline output on the trace span (`trace_span.record(output={"task_type": ..., "timings": timings, "answer_summary": ...})` — truncated; never the raw sheet data).
- **IMPLEMENT — per-step token cost + time on stage spans:** after each agent run, extract usage from the `AgentRunResult` and record on the corresponding stage span. pydantic-ai's `AgentRunResult.usage` property returns a `RunUsage` with `input_tokens`, `output_tokens`, and `cost` (best-effort USD via genai-prices; `None` if the model isn't priced). The existing `timed()` context manager already records elapsed seconds in the `timings` dict. Wire them together:
  - **Planner** (after line 747): `planner_span.record_usage(plan_result.usage, time_ms=timings.get("planner", 0) * 1000)`
  - **Pre_populate** (after line 777): `prepop_span.record(decision="pre_populated", output=pre_populated)` — no LLM tokens (pure Python); record `time_ms` only: `prepop_span.record_usage(None, time_ms=timings.get("pre_populate", 0) * 1000)` (the helper handles `usage=None` by recording `tokens_in: 0, tokens_out: 0, cost_usd: null`).
  - **Executor** (after line 1018): `executor_span.record_usage(exec_result.usage, time_ms=timings.get("executor", 0) * 1000)`
  - **Responder** (in `generate_user_friendly_response`, after line 1066): `responder_span.record_usage(result.usage, time_ms=...)` — note: the responder is only called via `generate_user_friendly_response` (legacy path); the main pipeline uses the executor's merged `friendly_response`, so responder tokens are usually zero. Still instrument it for completeness.
  - **Short-circuit paths** (pure-retrieve at 786, retrieve_numbers-items at 834): these skip the executor LLM — record `tokens_in: 0, tokens_out: 0, cost_usd: 0, time_ms: timings.get(...) * 1000` on the decision span so the user sees *why* there were no LLM tokens (the decision span explains "skip_executor" and the zero tokens confirm it).
- **IMPLEMENT — per-query totals on the trace span:** at the end of `run_pipeline` (before each `return` statement — there are 3 return points at lines 820, 949, 1041), aggregate and record on `trace_span`:
  ```python
  total_in = sum(s.get("tokens_in", 0) for s in stage_usages)
  total_out = sum(s.get("tokens_out", 0) for s in stage_usages)
  total_cost = sum(s["cost_usd"] for s in stage_usages if s.get("cost_usd") is not None)
  total_time_ms = sum(timings.values()) * 1000
  trace_span.record_totals(
      tokens_in=total_in, tokens_out=total_out,
      cost_usd=total_cost if total_cost else None,
      time_ms=total_time_ms,
  )
  ```
  Collect `stage_usages` as a list throughout the pipeline (append after each `record_usage` call). This makes `total_tokens_in`, `total_tokens_out`, `total_tokens`, `total_cost_usd`, and `total_time_ms` visible in the Langfuse trace header alongside the reasoning tree, tool calls, decisions, and input/outputs.
- **IMPLEMENT — tool calls in `_prepopulate_retrievals` (lines 620-674):** wrap each `retrieve_values(...)` call (both the cross-sheet branch at line 653 and the specific-sheet branch at line 658) in an `observe_step(name="excel-chat:tool:retrieve_values", input={"field": ..., "years": ..., "sheet": ...})` span, calling `proxy.set_output(raw)` with the returned string. These calls bypass the agent tool dispatcher, so without explicit spans their inputs/outputs would never appear in Langfuse. No tokens (pure Python) — record `time_ms` only.
- **IMPLEMENT — decision spans for short-circuits:** at each of the three paths record the decision + input + output:
  - Pure-retrieve short-circuit (lines 786-826): `observe_step(name="excel-chat:decision:short_circuit", input={"path": "pure_retrieve", "steps": list(plan.plan.keys())})`, then `proxy.record(decision="skip_executor", output=execution.step_results)`.
  - Retrieve-numbers-items short-circuit (lines 834-955): `observe_step(name="excel-chat:decision:short_circuit", input={"path": "retrieve_numbers_items", "items": plan.items})`, then `proxy.record(decision="skip_executor", output=step_results)`.
  - Full executor path (line 957+): `observe_step(name="excel-chat:decision:short_circuit", input={"path": "executor", "task_type": plan.task_type})`, then `proxy.record(decision="run_executor", output=execution.step_results)`.
- **IMPLEMENT — fallback attempts in `_run_with_fallback` (lines 485-537):** wrap each attempt (line 510-532) in `observe_step(name="excel-chat:agent_attempt", input={"stage": label, "model": model_label, "prompt": prompt[:500]}, metadata={"stage": label, "model": model_label})`; on success `proxy.set_output(result.output if hasattr(result, "output") else result)` AND `proxy.record_usage(result.usage, time_ms=...)` (each attempt's tokens + cost are visible, so the user can see e.g. "primary attempt: 1200 in / 300 out / $0.003, failed → fallback attempt: 1100 in / 280 out / $0.002, succeeded"); on exception `proxy.record(decision="retry", error=...)` before re-raising (the span proxy records `error` automatically on exception). This surfaces the fallback/retry decision tree with per-attempt cost in Langfuse (directly relevant to the recent 3-attempt retry work, `git log 8b6ada2`).
- **IMPORTS**: `from observability import observe_agent_run, observe_step` (module is flat in `backend/src/` — same import style as `from sheet_metadata import SheetMeta`, line 18).
- **GOTCHA**: `observe_agent_run` / `observe_step` must be importable without triggering langfuse import errors — keep langfuse imports lazy inside `observability.py`.
- **GOTCHA**: Never put full sheet contents or raw `pd.DataFrame` values in span input/output — truncate to ~500 chars and rely on `mask_pii` in `observe_step`.
- **GOTCHA**: pydantic-ai 2.30.0 may expose `.usage` as a property (preferred) or `.usage()` method (deprecated) — check during the VERIFY task and use whichever the installed version supports. The `record_usage` helper should handle both (try property first, fall back to method call).
- **GOTCHA**: `RunUsage.cost` is `None` for models genai-prices can't price (e.g. some OpenRouter models) — record `cost_usd: null` in that case, NOT `0.0`, so "unknown" stays distinguishable from "genuinely free" (per pydantic-ai docs).
- **GOTCHA**: The pipeline has 3 return points (lines 820, 949, 1041) — the totals recording must happen before EACH return, not just the last one. Use a helper or try/finally to avoid duplication.
- **VALIDATE**: `python -m py_compile backend/src/pipeline.py`

### UPDATE backend/src/main.py — all-endpoint middleware, request spans, decision spans, shutdown flush

- **IMPLEMENT**: Import `from observability import observe_agent_run, observe_step, flush_observability` (top of file, matching existing flat imports).
- **IMPLEMENT**: In `_lifespan` (line 96): add `flush_observability()` in the shutdown block (after the existing cleanup logic) so in-flight traces export before exit.
- **IMPLEMENT — all-endpoint request span via FastAPI middleware** (register right after `app = FastAPI(...)` at line 119, alongside the existing CORS middleware):
  ```python
  @app.middleware("http")
  async def langfuse_request_span(request: Request, call_next):
      user_id = request.headers.get("X-User-ID") or "anonymous"
      with observe_agent_run(
          name=f"excel-chat:http:{request.method} {request.url.path}",
          user_id=user_id,
          tags=["excel-chat", "http"],
          metadata={"method": request.method, "path": request.url.path},
      ) as span:
          response = await call_next(request)
          if span: span.record(output={"status_code": response.status_code})
          return response
  ```
  This ensures uploads, describe-sheet, threads, files, cache-clear, and query endpoints all produce a trace with method/path/status/user. Wrap the `call_next` in try/except to record 5xx errors as `error` metadata, then re-raise.
- **IMPLEMENT**: In `query_rag` (line 470) and `query_stream` (line 660): wrap the request body (after `user_id = x_user_id or "anonymous"` is resolved, lines 480 / 685) with:
  ```python
  with observe_agent_run(
      name="excel-chat:api:/query" | "excel-chat:api:/query/stream",
      user_id=user_id,
      tags=["excel-chat", "api"],
      metadata={"query": query[:500]},   # truncated; never log full sheets/PII
  ) as span:
  ```
  For `query_stream`, wrap inside `stream_generator()` so the SSE generator's lifetime is covered (end on generator close via try/finally). Record the final answer/friendly_response (truncated) as span output.
- **IMPLEMENT — guardrail decision spans:** around each `screen_query` call (main.py:483 and 696) add:
  ```python
  with observe_step(name="excel-chat:decision:guardrail", input={"query": query[:500]}) as grd:
      query_check = screen_query(query, user_id=user_id)
      grd.record(decision="allow" if query_check.allowed else "reject",
                 output={"allowed": query_check.allowed, "reason": query_check.reason,
                         "category": getattr(query_check, "category", None)})
  ```
- **IMPLEMENT — semantic cache decision spans:** around each cache lookup (main.py:500-535 and 702-764) add:
  ```python
  with observe_step(name="excel-chat:decision:semantic_cache", input={"query": query[:500]}) as cache_span:
      query_embedding = embed_query(query)
      cached_response, similarity = find_similar_cached(user_id, query_embedding, threshold=0.88, query_text=query)
      cache_span.record(decision="hit" if cached_response is not None else "miss",
                        output={"similarity": round(similarity, 4) if similarity else None,
                                "cached": cached_response is not None})
  ```
  (Keep the existing try/except so a cache failure never breaks the query — the span's exception handler records the error and re-raises into the existing handler.)
- **GOTCHA**: `/query` has multiple early returns (no sheets, cache hit at 516-531, errors at 644-653) — the context manager must wrap the whole `try:` so the span ends on every path; prefer wrapping the function body inside the existing `try:` rather than decorating, to keep control flow intact.
- **GOTCHA**: Do not put raw sheet contents or full `result` dicts in metadata — token budgets and PII (see `mask_pii`). Truncate strings (`.get("query", "")[:500]`).
- **GOTCHA**: The middleware span and the endpoint's `observe_agent_run` span nest (middleware → endpoint) — that is intended (request → pipeline hierarchy). Avoid double-recording the query text in both; keep the middleware metadata minimal (method/path/status only).
- **VALIDATE**: `python -m py_compile backend/src/main.py` then start the server briefly: `cd backend/src && uvicorn main:app --port 8000` (no keys set → expect `⚠️ Langfuse observability disabled` log and normal startup).

### UPDATE tests/test_langfuse_observability.py — rely on app instrumentation; assert decisions + tool I/O

- **IMPLEMENT**: Refactor `_run_query_traced` (lines 141-169) so it no longer wraps the pipeline in test-side `@observe`. Instead: call `init_observability()` once (module-level or in the `langfuse_available` fixture) and run `pipe(query)` directly; capture the user_id (already unique per test, e.g. `test-lf-{uuid}` at line 355) as the correlation key.
- **IMPLEMENT**: Replace `_fetch_trace_by_id`-based lookups (lines 208-229, used at 370 and 465) with fetching by filter: `langfuse.client.fetch_traces(user_id=user_id, limit=5)` (v4 client API) or `langfuse.api.traces.get_many(user_id=..., limit=5)` (same pattern as `tests/export_langfuse_traces.py:137-151`). Keep the 90s retry/backoff loop (lines 218-229) — ingestion is async (15-30s).
- **IMPLEMENT**: Assert the trace lands in the **"excel-chat" project**: verify the fetched trace's `project_id` matches the project bound to the configured key pair (the keys came from the "excel-chat" project's settings — this is a config sanity check, `assert trace.project_id == os.environ.get("LANGFUSE_PROJECT_ID")` only when the test sets/validates it).
- **IMPLEMENT**: In `test_langfuse_trace_capture` (lines 338-437), add assertions that verify the *application* instrumentation:
  - At least one observation of type `GENERATION` whose `model` attribute is non-empty (proves `instrument=True` on agents works — this is the assertion the file's own comment at line 397 anticipates).
  - Generation token usage present (input/output) — already asserted at 403-427; keep.
  - Span names include `excel-chat:*` (query/pipeline/agent spans).
  - **Tool-execution spans carry inputs AND outputs**: at least one SPAN observation whose name matches `excel-chat:tool:*` or a pydantic-ai tool span (e.g. `retrieve_values`, `execute_python_code`) with non-empty `input` and non-empty `output` fields (assert `getattr(span, "input", None) is not None` and `output is not None`).
  - **Decision metadata present**: at least one SPAN whose name contains `decision` (guardrail or short_circuit) has `metadata["decision"]` in `{"allow", "reject", "skip_executor", "run_executor", "hit", "miss", "retry"}`. For calculation queries, also assert `execute_python_code` tool span input contains the generated Python snippet and output contains the result.
  - **Per-step cost + time on stage spans**: at least one SPAN observation (e.g. `excel-chat:agent_attempt` or a stage span) has `metadata` containing `tokens_in` (int ≥ 0), `tokens_out` (int ≥ 0), and `time_ms` (float ≥ 0). For queries that invoke the executor (calculation queries), assert `tokens_in > 0` and `tokens_out > 0` on the executor stage span. For short-circuit (pure-retrieve) queries, assert `tokens_in == 0` and `tokens_out == 0` on the decision span (confirming no LLM was called).
  - **Per-query totals on the trace span**: the trace-level span (`excel-chat:query`) has `metadata` containing `total_tokens_in`, `total_tokens_out`, `total_tokens` (sum), `total_time_ms` (float > 0), and optionally `total_cost_usd` (float or null). Assert `total_tokens > 0` for queries that call any LLM, and `total_time_ms > 0` for all queries.
- **IMPLEMENT**: In `test_all_queries_traced` (lines 444-477), keep the distinct-trace assertion and additionally assert each trace has ≥1 decision span and ≥1 tool span with I/O (same helpers as above).
- **GOTCHA**: OTel-instrumented pydantic-ai runs export as traces whose IDs differ from any SDK-native `@observe` trace IDs. Fetching by `user_id` filter is the robust correlation method; do not rely on `get_current_trace_id()`.
- **GOTCHA**: `Agent.instrument_all()` is global and must run before the first agent build — call `init_observability()` at module import of the test file (after `load_dotenv()`, before `build_query_pipeline` is imported/used at line 68).
- **GOTCHA**: Pure-retrieve queries (q01-q15 mixes) may legitimately have no `execute_python_code` span — assert tool spans generically (`retrieve_values` at minimum, since every query retrieves), and only assert `execute_python_code` for queries that require computation.
- **VALIDATE**: With keys set: `python -m pytest tests/test_langfuse_observability.py -v -s -k "test_langfuse_trace_capture or test_all_queries_traced"` (expect gated pass; LLM + Langfuse keys required).

### CREATE tests/conftest.py — disable Langfuse in unit tests

- **IMPLEMENT**: Autouse session fixture that, unless `LANGFUSE_ENABLED_IN_TESTS=1` is set, pops `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` from `os.environ` before test collection. This guarantees unit tests never emit traces and exercise the graceful-degradation path (satisfies validate-observability.md section 8: "Verify conftest.py disables Langfuse in unit tests").
- **GOTCHA**: `test_langfuse_observability.py` explicitly needs keys — it must skip gracefully when stripped (its `langfuse_available` fixture already does `pytest.skip`, lines 188-201), and CI should set `LANGFUSE_ENABLED_IN_TESTS=1` for the integration run.
- **VALIDATE**: `python -m pytest tests/test_observability_unit.py tests/test_query_pipeline.py -q` — all pass with no network/langfuse activity.

### CREATE tests/test_observability_unit.py — offline coverage

- **IMPLEMENT**: Unit tests (no keys, no network):
  - `test_disabled_when_keys_missing` — `is_observability_enabled()` is False with env stripped; `observe_agent_run(...)` context manager is a no-op and does not raise; `flush_observability()` does not raise.
  - `test_enabled_when_keys_present` — set dummy keys in monkeypatch; `is_observability_enabled()` is True.
  - `test_mask_pii` — emails → `[EMAIL]`, `call 555-123-4567` → `[PHONE]`; plain text unchanged.
  - `test_mask_pii_only_in_production` — with `LANGFUSE_TRACING_ENVIRONMENT` unset/dev, `mask_pii` returns input unchanged.
  - `test_init_observability_idempotent` — calling twice does not raise (module flag guard).
  - `test_observe_step_records_input_and_output` — with a mocked langfuse client, `observe_step(name, input=...)` calls `span.update(input=...)` on entry and `proxy.set_output(...)` calls `span.update(output=...)` before exit (verify via a fake span object capturing calls).
  - `test_observe_step_records_error` — an exception inside `observe_step` records `metadata["error"]` on the fake span and re-raises.
  - `test_record_usage_extracts_tokens_and_cost` — pass a fake `RunUsage`-like object (with `input_tokens=100`, `output_tokens=50`, `cost=Decimal("0.003")`) to `proxy.record_usage(usage, time_ms=1234.0)`; assert the fake span received `metadata` with `tokens_in=100`, `tokens_out=50`, `total_tokens=150`, `cost_usd="0.003"`, `time_ms=1234.0`.
  - `test_record_usage_handles_none_cost` — `RunUsage.cost=None` → `cost_usd: null` in metadata (not 0.0).
  - `test_record_usage_handles_none_usage` — `proxy.record_usage(None, time_ms=500.0)` → `tokens_in=0, tokens_out=0, cost_usd=null, time_ms=500.0` (pure-Python stages with no LLM).
  - `test_record_totals` — `proxy.record_totals(tokens_in=200, tokens_out=100, cost_usd=0.005, time_ms=3000.0)` → fake span metadata has `total_tokens_in=200`, `total_tokens_out=100`, `total_tokens=300`, `total_cost_usd=0.005`, `total_time_ms=3000.0`.
- **PATTERN**: pytest fixture style from existing tests (`tests/test_optimization.py`, `tests/test_cache_service.py`); use `monkeypatch.setenv/delenv`; fake span objects (plain classes recording `update()` calls) for the `observe_step` tests.
- **GOTCHA**: Do not import `langfuse` at module top-level in the test if it may be absent in some envs — `observability.py`'s lazy imports handle this; tests should import `observability` only.
- **VALIDATE**: `python -m pytest tests/test_observability_unit.py -v`

### UPDATE tests/export_langfuse_traces.py — default to the excel-chat project/tag

- **IMPLEMENT**: Change the `--tag` default from `None` to `"excel-chat"` (line ~390) so exports always pull from the right project's traces by default; keep `--user-prefix`/`--session-id` overrides as-is. Optionally add `--project-id` passthrough if the v4 API supports it (`langfuse.api.traces.get_many(project_id=...)`).
- **VALIDATE**: `python -m py_compile tests/export_langfuse_traces.py`

### UPDATE plans/agent-architecture.md — document the observability layer (optional but recommended)

- **IMPLEMENT**: Extend section 13 ("Timing & Observability") with a short subsection: Langfuse v4 OTel-based integration, **project routing via "excel-chat"-scoped API keys**, env vars, the `observability.py` module, span taxonomy (http request → api/query → pipeline stages → tool calls → LLM generations + decision spans), the decisions/tool-input/tool-output capture contract, graceful degradation policy, and the conftest rule.
- **VALIDATE**: Render check — file is markdown; no command needed (visual review).

---

## TESTING STRATEGY

### Unit Tests

`tests/test_observability_unit.py` — offline, zero network, zero keys: env parsing, graceful degradation no-ops, PII masking (dev vs production), idempotent init. Follow existing pytest style (fixtures, monkeypatch).

### Integration Tests

`tests/test_langfuse_observability.py` (exists, update): 15 real financial queries against `example_sheets/Detailed_Expense_Breakdown.xlsx`, gated on `OPENROUTER_API_KEY` + `LANGFUSE_*` keys. Verify per-query traces via `user_id` filter with 90s retry/backoff (async ingestion) **in the "excel-chat" project**, assert generations have model + token usage + latency, **tool-execution spans carry inputs and outputs**, **decision spans (guardrail / short-circuit / cache) carry `metadata["decision"]`**, **stage spans carry `tokens_in`/`tokens_out`/`cost_usd`/`time_ms`**, **trace span carries `total_tokens`/`total_cost_usd`/`total_time_ms`**, and that traces are distinct (no dedup/collision — `test_all_queries_traced`).

### Edge Cases

- Missing `LANGFUSE_*` keys in production → app fully functional, one `⚠️` log line, no crash.
- Langfuse API down / bad keys / network failure mid-query → query completes; observability errors swallowed; the failed span records `metadata["error"]` and the query result is unaffected.
- SSE streaming: span must close when the generator finishes OR when the client disconnects (wrap in try/finally).
- Early returns in `query_rag` (cache hit at 516, no-sheets at 545, guardrail rejection at 486) → span still closes with correct decision metadata (guardrail reject reason, cache hit + similarity).
- OTel trace IDs vs SDK trace IDs → never correlate via `get_current_trace_id()`; use user/session filters.
- `Agent.instrument_all()` called twice / agent built before init → init must be idempotent and called before any agent construction (module import in pipeline.py is the enforcement point).
- Non-agent tool calls (`_prepopulate_retrievals`) → must still appear as `excel-chat:tool:retrieve_values` spans with args + values (explicit spans, since pydantic-ai instrumentation doesn't cover them).
- PII/token bloat in span input/output → all `observe_step` input/output goes through `mask_pii` + 500-char truncation; never embed full DataFrames or raw sheet contents.
- Keys belong to the WRONG project → traces silently land elsewhere; the integration test's project_id sanity check (and manual UI check) catches this.

---

## VALIDATION COMMANDS

Run from repo root (`capstone/excel-chat`) unless noted. Tests run against `backend/src` via the existing `sys.path` pattern.

### Level 1: Syntax & Style

```
python -m py_compile backend/src/observability.py backend/src/pipeline.py backend/src/main.py
```

### Level 2: Unit Tests (no keys needed)

```
python -m pytest tests/test_observability_unit.py -v
python -m pytest tests/test_query_pipeline.py tests/test_fallback_mechanism.py tests/test_optimization.py -q
```

### Level 3: Integration Tests (requires OPENROUTER_API_KEY + LANGFUSE_* keys)

```
LANGFUSE_ENABLED_IN_TESTS=1 python -m pytest tests/test_langfuse_observability.py -v -s
```

### Level 4: Manual Validation

- Env check: `python -c "import os; print(all(os.getenv(k) for k in ['LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY','LANGFUSE_BASE_URL']))"` → True.
- Connectivity (from validate-observability.md section 2): run the `get_client()` + span snippet → "Langfuse connectivity: OK".
- **Project routing check:** `python -c "from langfuse import get_client; print(get_client().auth_check())"` → True, and the project bound to the keys in the Langfuse UI is named **excel-chat**.
- Start backend: `cd backend/src && uvicorn main:app --reload`; with keys → no warnings, traces flow; without keys → `⚠️ Langfuse observability disabled` and normal operation.
- Run a query via `GET /query/stream?query=...&X-User-ID=test1` and confirm in the **"excel-chat" project** UI: trace named `excel-chat:query` (or `excel-chat:api:/query/stream`) with child spans planner/pre_populate/executor + LLM generations with token usage and latency; user `test1`; tags `excel-chat`.
- **Trace content check (the core requirement):** open the trace and verify (a) tool spans (e.g. `retrieve_values`, `execute_python_code`) show their input arguments AND output values; (b) decision spans (`excel-chat:decision:*`) show `decision` + `output` metadata (guardrail allow/reject with reason; short-circuit skip_executor/run_executor; cache hit/miss with similarity); (c) fallback attempt spans show model + error when retries occur; (d) **each stage span shows `tokens_in`, `tokens_out`, `cost_usd`, and `time_ms` in its metadata** (e.g. planner: 800 in / 200 out / $0.002 / 1500ms); (e) **the trace header shows `total_tokens`, `total_cost_usd`, and `total_time_ms`** aggregated across all stages (e.g. total: 2000 in / 500 out / $0.007 / 4200ms).
- Also hit a non-query endpoint (e.g. `GET /sheets/`) and confirm an `excel-chat:http:GET /sheets/` span appears (middleware coverage).
- Export traces: `python tests/export_langfuse_traces.py --user-prefix test-lf- --limit 20` → `langfuse_exports/summary.md` shows generations per query (default tag now `excel-chat`).

### Level 5: Additional Validation (Optional)

- `core_piv_loop/validate-observability.md` sections 1-10 as the formal checklist (adapt `src/shared/observability.py` → `backend/src/observability.py`).
- Troubleshoot missing traces with `LANGFUSE_DEBUG=True` per the official doc troubleshooting section.

---

## ACCEPTANCE CRITERIA

- [ ] **Project routing:** `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` are generated from the **"excel-chat"** Langfuse project; every trace this feature emits appears under that project (verified via UI + `auth_check()` + project_id sanity check in tests).
- [ ] `backend/src/observability.py` implements init/observe_step/observe/flush/mask with graceful degradation; no module-import-time side effects when langfuse is absent.
- [ ] All 3 pydantic-ai agents constructed with instrumentation (`instrument=True` or capability equivalent, warning-free on installed pydantic-ai 2.30.0).
- [ ] Every `/query` and `/query/stream` request produces a trace with `user_id`, tags, and query metadata; pipeline stages and fallback attempts appear as spans.
- [ ] **Tool calls with inputs + outputs:** agent tool executions (retrieve_values, execute_python_code, EDA tools) AND the direct `retrieve_values` calls in `_prepopulate_retrievals` appear as spans carrying their input arguments and output values.
- [ ] **Decisions recorded:** guardrail allow/reject (+reason), semantic cache hit/miss (+similarity), short-circuit choice (skip_executor/run_executor), and fallback attempts (model + error) are written to span metadata with a `decision` key.
- [ ] **Per-step token cost + time:** each pipeline stage span (planner, pre_populate, executor, responder, agent_attempt) carries `tokens_in`, `tokens_out`, `cost_usd`, and `time_ms` in its metadata — extracted from pydantic-ai `RunUsage` and the `timed()` dict.
- [ ] **Per-query totals:** the trace-level span (`excel-chat:query`) carries `total_tokens_in`, `total_tokens_out`, `total_tokens`, `total_cost_usd`, and `total_time_ms` — aggregated across all stages, visible in the Langfuse trace header alongside the reasoning tree.
- [ ] **All endpoints traced:** FastAPI middleware produces an `excel-chat:http:*` span for every request (method, path, status, user_id), including uploads/threads/files/describe-sheet.
- [ ] `langfuse.flush()` runs on FastAPI shutdown.
- [ ] With no `LANGFUSE_*` keys: full test suite + app run with zero trace emission and zero crashes (graceful degradation).
- [ ] `tests/test_observability_unit.py` passes offline (incl. `observe_step` input/output/error recording via fake spans, `record_usage` token/cost extraction incl. `None` cost, `record_totals` aggregation); `tests/conftest.py` strips keys for unit runs.
- [ ] `tests/test_langfuse_observability.py` passes with keys set and verifies agent-instrumented GENERATION observations (model + token usage), tool spans with input/output, decision-span metadata, **stage spans with `tokens_in`/`tokens_out`/`cost_usd`/`time_ms`**, **trace span with `total_tokens`/`total_cost_usd`/`total_time_ms`**, distinct traces per query.
- [ ] `tests/export_langfuse_traces.py` produces a readable summary for the 15 queries (default tag `excel-chat`).
- [ ] No regressions: existing test suite passes (`pytest tests/ -q`, keys-stripped path).
- [ ] `plans/agent-architecture.md` observability section updated.
- [ ] All validation commands pass with zero errors.

---

## COMPLETION CHECKLIST

- [ ] All tasks completed in order
- [ ] Each task validation passed immediately
- [ ] All validation commands executed successfully
- [ ] Full test suite passes (unit + integration-gated)
- [ ] No linting or type checking errors (py_compile clean; frontend untouched)
- [ ] Manual testing confirms traces appear in Langfuse UI with correct structure
- [ ] Acceptance criteria all met
- [ ] Code reviewed for quality and maintainability

---

## NOTES

**Design decisions & trade-offs:**

- **Project routing = key pair, not a config flag.** Langfuse routes traces by which project the API keys belong to. All traces go to "excel-chat" by using a key pair created in that project's Settings → API Keys. No code change can override this — it's enforced at setup time and sanity-checked by tests (project_id assertion) and manual UI verification.
- **v4 OTel integration over the old v3 wrapper.** The repo already pins `langfuse>=4.0.0,<5`, so we use `Agent.instrument_all()` + `instrument=True` (the official v4 pydantic-ai pattern) rather than the removed `langfuse.pydantic_ai.Langfuse` wrapper. LLM generations, token usage, and per-agent-tool spans (with inputs/outputs) come for free from pydantic-ai's OTel instrumentation.
- **Explicit spans for non-agent paths.** `_prepopulate_retrievals` calls `retrieve_values` directly (not through the agent tool dispatcher), so it needs explicit `observe_step` spans to satisfy the "tool call + input + output" requirement. Decision spans (guardrail, cache, short-circuits, fallback attempts) are also explicit because they're app logic, not agent activity.
- **Flat module location.** validate-observability.md (from a generic guide) expects `src/shared/observability.py`; this repo is flat (`backend/src/`), so the module lives at `backend/src/observability.py`. Update the validation doc references when running the checklist.
- **Span taxonomy:** `excel-chat:http:*` (middleware, all endpoints) → `excel-chat:api:/query*` (request) → `excel-chat:query` (pipeline) → `excel-chat:step:*` / `excel-chat:tool:*` / `excel-chat:decision:*` / `excel-chat:agent_attempt` (per fallback attempt) → OTel GENERATION observations (LLM calls). Existing `timed()` dict remains the client-facing timing source; Langfuse is the deep-dive debugging source.
- **Decision contract:** every decision span carries `metadata["decision"]` ∈ {allow, reject, hit, miss, skip_executor, run_executor, retry} plus `output` with the supporting detail (reason, similarity, step_results, model/error) — machine-queryable via Langfuse analytics without parsing free text.
- **Cost/time contract:** every stage span carries `metadata` with `tokens_in`, `tokens_out`, `total_tokens`, `cost_usd` (USD float or `null`), and `time_ms` — extracted from pydantic-ai's `RunUsage` (which uses genai-prices for best-effort cost) and the existing `timed()` dict. The trace-level span carries the aggregated `total_*` variants. This means cost + time are visible *on the same span* as the reasoning step's decision, tool calls, and input/output — no separate "cost trace" to cross-reference. `cost_usd: null` (not `0.0`) signals "model not priced by genai-prices" so unknown cost stays distinguishable from genuinely free (per pydantic-ai docs). Short-circuit paths (pure-retrieve) record `tokens_in: 0, tokens_out: 0, cost_usd: 0` — confirming the LLM was skipped, which is the whole point of the optimization.
- **Correlation strategy:** correlate traces by `user_id` (tests already generate unique ids like `test-lf-{uuid}`) and tags, NOT by `get_current_trace_id()`, because OTel-exported traces carry OTel trace IDs.
- **Cost/latency analysis:** with per-step `tokens_in`/`tokens_out`/`cost_usd`/`time_ms` on stage spans and `total_*` on the trace span, per-query cost and per-stage latency are visible directly in the Langfuse trace tree (no separate analytics query needed) AND queryable in Langfuse analytics for aggregation; the 15-query integration test doubles as a benchmark set (q09/q14 duplicate intentionally kept — per existing test comment). Per-attempt cost on fallback spans shows exactly how much each retry cost.
- **Out of scope:** OpenTelemetry *auto-instrumentation* of FastAPI/httpx (third-party OTel spans count toward billable units per the official FAQ) — we write our own minimal middleware span instead; Langfuse scores/evals, prompt management, and dashboard configuration.
- **Known issue to watch:** if pydantic-ai 2.30.0 deprecation-warns on `instrument=True`, use `capabilities=[Instrumentation(settings=InstrumentationSettings(include_binary_content=False))]` (per langfuse GitHub issue #13790, `include_binary_content=False` avoids lost traces with binary content).
- **Confidence score: 7/10** — the integration pattern is documented and pinned deps match, but exact v4 API surface (env var names, `propagate_attributes` availability, pydantic-ai capability API, span input/output field naming in the v4 client) must be verified against the installed versions during Task 2 before wiring.
