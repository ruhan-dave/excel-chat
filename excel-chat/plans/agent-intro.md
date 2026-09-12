# Excel-Chat Agent — Architecture Overview

One Pydantic AI agent handles the full query lifecycle: plan, execute, synthesize.
No separate planner/executor/responder agents. Code-enforced validation at three gates.

## Entry Point (`main.py`)

```
GET /query/stream (SSE)     GET /query (JSON)
        │                          │
        ▼                          ▼
  guardrail: screen_query()  ── reject → 400
        │
        ▼
  semantic cache: embed + find_similar_cached()  ── hit → return cached
        │
        ▼
  load sheets from S3 (per s3_key cache)
        │
        ▼
  build_query_pipeline(sheets, sheet_metas, user_id, on_event)  ← pipeline.py
```

## Pipeline Orchestration (`pipeline.py` — `run_pipeline`)

1. **Deterministic short-circuit** (0 LLM calls) — fires only when the query is an
   unambiguous single-field, single-year lookup with no calculation keywords.
   Calls `retrieve_values` directly → `ExecutionResult` → done.
2. **Agent run** — `_run_with_fallback` wraps the agent call with model fallback:
   - Attempt 1: `deepseek/deepseek-v4-flash` (primary)
   - Attempt 2: `openai/gpt-oss-120b:nitro` (fallback, on 5xx/timeout/empty)
   - Attempt 3: primary again (2s, 4s backoff)
3. **Output validation loop** — `validate_execution_result` checks the structured
   output; on failure, errors are sent back with `message_history` and the agent
   re-runs (up to 2 retries). Deterministic friendly-response formatters are
   last-resort fallbacks when the model leaves `friendly_response` empty.

## Agent Construction (`agent.py` — `build_query_agent`)

Everything data-specific is injected at build time into the system prompt:

| Injected | Source | Purpose |
|----------|--------|---------|
| `model` | `build_openrouter_model()` | OpenRouter client (`MODEL_ID` env) |
| `sheet_context` + `cross_sheet_note` | `_build_sheet_context(sheet_metas)` | Sheet catalog baked into prompt |
| `all_fields`, `all_years` | derived from `sheet_metas` | Exact names the model must use |
| `tools` | dynamic selection (below) | 6 or 21 tools depending on query |
| `deps_type=PipelineDeps` | `tools.py` | Runtime: sheets, on_event, plan, user_id |
| `output_type=ExecutionResult` | `models.py` | Structured output contract |
| `capabilities` | pydantic-ai | Thinking + Instrumentation + ProcessHistory |

**Runtime injection** (per query, via `RunContext`): `PipelineDeps` carries the
DataFrames, SSE callback, sheet metadata, the accepted plan, and user_id. Tools
access everything through `ctx.deps` — nothing data-specific is baked in.

**Dynamic tool selection** (`_is_eda_query`): keyword scan on the query.
- EDA queries → 17 EDA tools + 2 core + 2 plan tools = 21 total
- Financial queries → `retrieve_values` + `execute_python_code` + 2 plan tools = 6 total

Cuts ~2-3k tokens of tool definitions for the common case.

## System Prompt (three sections)

1. **Catalog** — every sheet with fields (first 20), years, schema group, description.
   Cross-sheet note: sheets sharing a schema group can be compared; omit sheet name
   to search the group.
2. **Workflow** — the 3-phase contract:
   - **PLAN FIRST**: call `write_plan` with a `QueryPlan`. Plan is validated and
     will be rejected if fields/years/actions/refs are wrong.
   - **EXECUTE**: batch years per retrieve call, use `execute_python_code` for math,
     mark steps with `update_step_status`.
   - **ANSWER**: return `ExecutionResult` with every step's result, real field names
     in `friendly_response`, explicit note if retrieval fails.
3. **Guardrails** — `GUARDRAIL_SYSTEM_PROMPT` (financial-advice disclaimer policy).

---

## Layer 1 — Schema Validation (Pydantic, automatic)

When the model calls `write_plan(plan: QueryPlan)`, Pydantic validates the
argument **before the tool body runs**. This is the first gate — it rejects
malformed plans at the framework level, no custom code needed.

### `PlanStep` schema (`models.py:26`)

```python
class PlanStep(BaseModel):
    action: Literal[
        "retrieve", "compute",
        "add", "subtract", "multiply", "divide", "return_percentage",
        "sqrt", "power", "log", "exp", "abs", "negate",
        "max", "min", "average", "median", "stdev",
        "yoy_growth", "cagr", "ratio", "percentage_change", "difference",
    ]
    args: list[str]
    description: str  # UI label, e.g. "Retrieving social security benefits 2017-2021"
```

- **`action`** is a `Literal` of 23 allowed values. Any other string → Pydantic
  raises `ValidationError`, the tool call fails, and the model retries automatically
  (`retries=2` on the Agent).
- **`args`** is always a list of strings. For `retrieve`: `["FieldName", "Year1",
  "Year2", ...]` or `["SheetName", "FieldName", "Year1", ...]`. For named ops:
  `["step1", "step2"]` (step references) or `["step1", "100"]` (mixed refs +
  literals). For `compute`: a natural-language description string.
- **`description`** defaults to `""` — the model can omit it, but the system
  prompt encourages it for the UI plan card.

### `QueryPlan` schema (`models.py:68`)

```python
class QueryPlan(BaseModel):
    task_type: Literal["retrieve_numbers", "perform_calculations", "give_advice", "other"]
    plan: dict[str, PlanStep] | None       # for perform_calculations
    items: list[str] | None                # for retrieve_numbers
    description: str | None               # for give_advice
```

- **`task_type`** is a `Literal` of 4 values. Unknown types → rejected.
- **`plan`** is `None` for `retrieve_numbers`/`give_advice`; a dict of `stepN` →
  `PlanStep` for `perform_calculations`. A `@field_validator` (`_parse_plan`)
  accepts both a list of step dicts and a pre-keyed dict, normalizing either to
  `{"step1": {...}, "step2": {...}, ...}`.
- **`items`** is `None` unless `task_type == "retrieve_numbers"`. Each item is a
  string like `"Social security benefits, 2023"` or `"Sheet1, Revenue, 2020"`.
  A `@field_validator` (`_parse_items`) accepts a JSON string or a list.

### `ExecutionResult` schema (`models.py:135`)

```python
class ExecutionResult(BaseModel):
    step_results: dict[str, Any]      # every step's result
    final_answer: Any                 # the computed/retrieved answer
    explanation: str = ""             # how the answer was derived
    friendly_response: str = ""       # user-facing natural language
```

- **`step_results`** and **`final_answer`** pass through a `@field_validator`
  (`_flatten_nested_objects`) that converts dict/list values to JSON strings via
  `_flatten_value`. This prevents the frontend from showing `[object Object]`.
  A dict like `{"value": 42}` with only a `value` key is unwrapped to `42`.
- **`final_answer`** is `Any` — it can be a number, a string, a dict, or a list.
  The output validator (Layer 3) checks its consistency with the terminal step.
- **`friendly_response`** is a plain string. The output validator checks that it
  references real field names from the catalog.

### What Pydantic catches automatically

| Violation | Example | Result |
|-----------|---------|--------|
| Unknown action | `action: "cagr_calc"` | `ValidationError`, tool call fails |
| Wrong task_type | `task_type: "forecast"` | `ValidationError`, tool call fails |
| Missing required field | `action` omitted | `ValidationError`, tool call fails |
| Wrong type for args | `args: "step1"` (string, not list) | `ValidationError`, tool call fails |
| Malformed plan dict | `plan: "step1"` (string) | `_parse_plan` tries to parse; if it fails, Pydantic rejects |

Pydantic does **not** catch: wrong field names, wrong years, wrong arg counts
for named ops, forward step references, or duplicate retrievals. Those are
Layer 2.

---

## Layer 2 — Plan Semantic Validation (`validate_plan_semantics`)

Inside `write_plan`, after Pydantic accepts the schema, `validate_plan_semantics`
runs pure-Python checks against the actual sheet catalog. This is the second gate.

### Setup

```python
valid_fields = {f for m in sheet_metas for f in m.fields}
valid_years = {str(y) for m in sheet_metas for y in m.years}
sheet_names = {m.sheet_name for m in sheet_metas}
```

These sets are the ground truth. Every field name, year, and sheet name in the
plan must match exactly.

### Check 0 — `retrieve_numbers` items format

For `task_type == "retrieve_numbers"`, each item in `plan.items` is parsed:

- Split on commas. Must be 2 parts (`"FieldName, Year"`) or 3 parts
  (`"SheetName, FieldName, Year"`).
- **2-part**: `field, year` — field must be in `valid_fields`, year in `valid_years`.
- **3-part**: `sheet, field, year` — sheet must be in `sheet_names`, field in
  `valid_fields`, year in `valid_years`.
- Wrong part count, unknown field, unknown year, or unknown sheet → error with
  the exact value and a suggestion (`_sample_from` uses `difflib` to find the
  closest catalog match, e.g. "did you mean 'Revenue'?").

Example rejection:
```
item 1: unknown field 'Revenu'. Use exact field names (did you mean 'Revenue'?).
item 2: unknown year '2025'. Valid years: ['2011', '2012', ..., '2023']
```

### Check 1 — Per-step validation (`perform_calculations`)

For each `stepN` in `plan.plan`:

**`retrieve` steps:**
- `args[0]` might be a sheet name. If it is, `args[1]` is the field and
  `args[2:]` are years. Otherwise `args[0]` is the field and `args[1:]` are years.
- Field must be in `valid_fields`. Unknown field → error with `_sample_from`
  suggestion.
- Each year must be in `valid_years`. Unknown years → error listing them.
- The field + step IDs are tracked for the multi-year batching check (Check 2).

**Named operations (not `retrieve`, not `compute`):**
- Arg count is validated against `_OP_ARG_COUNTS`:

  | Category | Operations | Expected args |
  |----------|-----------|---------------|
  | Unary (1) | `sqrt`, `abs`, `negate`, `exp` | exactly 1 |
  | Binary (2) | `subtract`, `divide`, `return_percentage`, `power`, `log`, `yoy_growth`, `ratio`, `percentage_change`, `difference` | exactly 2 |
  | Ternary (3) | `cagr` | exactly 3 (`[end_value, start_value, num_years]`) |
  | N-ary (2+) | `add`, `multiply`, `max`, `min`, `average`, `median`, `stdev` | 2 or more |

  Wrong count → error: `"step3: 'cagr' expects exactly 3 arg(s), got 2. Args: ['step1', 'step2']"`.

- Each arg must be either:
  1. A reference to a **prior** step (e.g. `"step1"`) — forward references are
     rejected: `"step4: references 'step5' which is defined later"`.
  2. A numeric literal (e.g. `"100"`, `"5"`).
  3. Anything else → error: `"step3: arg 'Revenue' must reference a prior step
     (e.g. 'step1') or be a literal number"`.

**`compute` steps:**
- No arg-count or arg-format validation — `compute` takes a natural-language
  description and runs arbitrary Python in the sandbox. The model is trusted to
  write correct code.

### Check 2 — Multi-year batching (anti-pattern detection)

After all steps are validated, the validator checks for duplicate retrievals:

```python
retrieve_fields: dict[str, list[str]] = {}  # field → list of step_ids
```

If the same field appears in 2+ `retrieve` steps, the validator rejects:

```
Multiple retrieve steps for 'Revenue': ['step1', 'step2'].
Combine into ONE retrieve step with multiple years:
['Revenue', 'Year1', 'Year2', ...].
```

This prevents the model from generating:
```
step1: retrieve ["Revenue", "2018"]
step2: retrieve ["Revenue", "2019"]
```
when it should be:
```
step1: retrieve ["Revenue", "2018", "2019"]
```

### Rejection feedback format

All errors are collected and returned as a single message:

```
PLAN REJECTED — fix these and call write_plan again:
- step1: unknown field 'Revenu'. Use exact field names (did you mean 'Revenue'?).
- step3: 'cagr' expects exactly 3 arg(s), got 2. Args: ['step1', 'step2']
- Multiple retrieve steps for 'Revenue': ['step1', 'step2']. Combine into ONE retrieve step with multiple years: ['Revenue', 'Year1', 'Year2', ...].
```

The feedback is **minimal and direct** — it tells the model exactly what to fix
without repeating the full sheet catalog. The model is expected to call
`write_plan` again with corrections. Execution does not proceed until the plan
is accepted.

On acceptance:
- Plan stored in `ctx.deps.plan`.
- `plan` SSE event emitted (UI shows the plan card).
- Acceptance message returned: `"Plan accepted (5 steps). Execute each step now,
  then return the final answer."`

---

## Layer 3 — Output Validation (`validate_execution_result`)

After the agent finishes executing and returns an `ExecutionResult`, the pipeline
calls `validate_execution_result` to check the structured output. This is the
third gate.

### Setup

```python
valid_fields = {f for m in sheet_metas for f in m.fields}
step_results = execution.step_results or {}
final = execution.final_answer
friendly = execution.friendly_response or ""
```

Missing-data markers are precomputed:
```python
_MISSING_DATA_MARKERS = (
    "not available", "no data", "unavailable", "missing",
    "could not be retrieved", "cannot be retrieved", "not found",
    "is not present", "are not present", "no value",
)
reported_missing = any(m in friendly_lower for m in _MISSING_DATA_MARKERS)
```

A helper identifies source-error results:
```python
def _is_source_error(val: Any) -> bool:
    return isinstance(val, str) and (val.startswith("ERROR:") or not val.strip())
```

### Check 1 — `step_results` completeness

**For `perform_calculations` plans** (`plan.plan` is a dict of steps):
- Every `stepN` in `plan.plan` must appear in `step_results`. Missing steps →
  error: `"step_results is missing these plan steps: ['step3', 'step4']"`.
- Steps that are present but empty/`ERROR:` are flagged. These are acceptable
  **only if** `friendly_response` explicitly reports the data as missing (via
  `_MISSING_DATA_MARKERS`). Otherwise → error: `"step_results has empty/ERROR
  values for steps: ['step2']. If the source data is genuinely unavailable,
  keep the ERROR value AND say so in friendly_response. Otherwise re-run the
  retrieval."`

**For `retrieve_numbers` plans** (`plan.items` is a list):
- Expected step IDs are `step1..stepN` where N = `len(plan.items)`.
- Missing steps → error: `"step_results is missing item steps: ['step1', ...].
  Expected ['step1', ...] for the 8 items in the plan."`

**For `give_advice`/`other` plans** (`plan` is None or has no steps):
- This check is skipped — steps are not plan-bound.

### Check 2 — `final_answer` consistency

If `final_answer` is `None` or empty:
- Acceptable **only if** `reported_missing` is True (the agent told the user the
  data is unavailable).
- Otherwise → error: `"final_answer is empty/None. Set it to the computed or
  retrieved value — or, if the source data is unavailable, say so explicitly in
  friendly_response."`

If `final_answer` is non-empty, the validator finds the **terminal step** — the
last non-`retrieve` step in the plan (the computation that produces the final
value). If all steps are `retrieve` (the model computed via tool calls outside
the plan), this check is skipped.

When a terminal step exists, the validator compares `final_answer` to the
terminal step's result. The comparison is **relaxed** to avoid forcing retries
for format-only mismatches:

**Number extraction:**
```python
def _norm_num(v) -> float | None:
    s = str(v).strip().rstrip("%").replace(",", "").replace("$", "")
    return float(s) if parseable else None

def _extract_num(v) -> float | None:
    # Regex extracts first number from strings like "-9.24 percentage points"
    m = re.search(r"-?\d+\.?\d*", str(v))
    return float(m.group()) if m else None
```

If `_norm_num` can't parse `step_val` directly (e.g. it's `"-9.24 percentage
points"`), `_extract_num` pulls the first number.

**Comparison logic:**

| `final_answer` type | `step_val` type | Rule |
|---------------------|-----------------|------|
| Numeric | Numeric | `abs(fn - sv) > max(abs(sv) * 0.01, 0.01)` → mismatch |
| String | Numeric | Substring match: generate formatted variants of `sv_num` (raw, abs, ×100 for percentage, comma-formatted, 1-2 decimal places) and check if any appears in `final_answer` (commas stripped from `final_answer` too) |
| String | Dict (JSON-flattened) | Parse the JSON, extract all scalar values (numbers and strings ≥3 chars), generate formatted variants for numbers (including ×100 for percentage), check if any appears in `final_answer`. If none match but `final_answer` is a non-empty qualitative statement, **accept it** — the step results are in the model's context and forcing a retry wastes a round-trip. |
| String | String (non-JSON) | Strict string compare |

**Formatted variants generated for a numeric `sv_num`:**
```python
formatted_variants = [
    str(sv_num), str(abs_sv),                    # raw + absolute value
    f"{sv_num:.1f}", f"{sv_num:.2f}",            # 1-2 decimal places
    f"{abs_sv:.1f}", f"{abs_sv:.2f}",            # abs, 1-2 decimals
    f"{sv_num:,.1f}", f"{sv_num:,.2f}",          # comma-formatted
    f"{abs_sv:,.1f}", f"{abs_sv:,.2f}",          # abs, comma-formatted
    f"{pct_sv:.1f}", f"{pct_sv:.2f}",            # ×100 (fraction → percentage)
    f"{abs_pct_sv:.1f}", f"{abs_pct_sv:.2f}",    # abs ×100
    f"{pct_sv:,.1f}", f"{pct_sv:,.2f}",          # ×100, comma-formatted
    f"{abs_pct_sv:,.1f}", f"{abs_pct_sv:,.2f}",  # abs ×100, comma-formatted
]
```

This handles all observed format-only mismatches:
- **Sign**: step `-9.24`, answer `"9.24 percentage points faster"` → `abs_sv` = `"9.24"` matches.
- **Percentage/fraction**: step `0.0924`, answer `"9.24%"` → `pct_sv` = `"9.24"` matches.
- **Commas**: step `7966.91`, answer `"7,966.91"` → comma-stripped answer matches `str(sv_num)`.
- **String-wrapped**: step `"-9.24 percentage points"`, `_extract_num` gets `-9.24`, then `abs_sv` = `"9.24"` matches.

**Genuinely wrong numbers are still caught**: step `-3718.49`, answer `"99,999.99"` → no variant of `3718.49` appears in the answer → mismatch → error.

### Check 3 — `friendly_response` field names

`friendly_response` must reference at least one real field name from the sheet
catalog. This rejects generic phrasing like `"the first series grew at 25.4%"`
that the old executor prompt warned about.

```python
referenced = [f for f in valid_fields if len(f) >= 4 and f.lower() in friendly_lower]
if not referenced:
    # Try shorter field names (≥3 chars), filtering common words
    referenced = [f for f in valid_fields if len(f) >= 3 and f.lower() in friendly_lower]
    referenced = [f for f in referenced if f.lower() not in common_words]
if not referenced:
    errors.append("friendly_response does not reference any actual field name...")
```

Common words filtered: `the, and, for, was, are, all, new, old, sum, avg, max, min`.

If `friendly_response` is empty → separate error: `"friendly_response is empty.
Provide a natural-language answer using actual field names from the sheet catalog."`

### Failure and retry

On failure, all errors are collected and sent back to the agent via
`_run_with_fallback` with `message_history` preserved. The model sees its own
prior work + the rejection message and re-runs (up to 2 retries). Example
rejection feedback:

```
OUTPUT REJECTED — fix these and return ExecutionResult again:
- step_results is missing these plan steps: ['step3', 'step4']. Every step in the plan must have a result in step_results.
- final_answer (0%) does not match the terminal step step4 result ({"operation": "return_percentage", "result": 0.0}). final_answer should equal or contain the last step's output.
```

If the output is still invalid after 2 retries, the pipeline proceeds
best-effort with whatever the model produced (the result is still returned to
the user, just without the validation guarantee).

---

## Streaming (SSE) — Real-Time Progress

The `/query/stream` endpoint sends Server-Sent Events to the frontend as the
agent works, so the user sees progress immediately rather than waiting for the
full response.

### Architecture

```
main.py stream_generator()
    │
    ├── pipeline_task = asyncio.create_task(pipeline(query))  ← runs in background
    │
    └── while not (pipeline_task.done() and queue.empty()):
            event_type, payload = await event_queue.get(timeout=0.1)
            yield f"event: {event_type}\ndata: {payload}\n\n"
```

The pipeline runs as a background `asyncio.Task`. Tools emit events through
`ctx.deps.emit(event_type, data)`, which pushes into an `asyncio.Queue`. The
stream generator drains the queue and yields SSE events immediately — the user
sees each event as it happens, not all at the end.

### `PipelineDeps.emit` (`tools.py:143`)

```python
def emit(self, event_type: str, data: dict[str, Any]) -> None:
    if self.on_event:
        try:
            self.on_event(event_type, data)
        except Exception:
            pass  # observability/progress must never break a query
```

The callback is injected at pipeline build time. `emit` swallows all callback
errors so a broken SSE connection cannot crash the agent.

### SSE event types (in arrival order)

| Event | When | Payload |
|-------|------|---------|
| `status` | Pipeline starts | `{"message": "Analyzing your question…"}` |
| `cached` | Semantic cache hit | Cached result (pipeline exits early) |
| `plan` | `write_plan` accepted | `{"task_type": ..., "plan": {...}, "items": [...]}` |
| `step_started` | `update_step_status("stepN", "in_progress")` | `{"step_id": "step1"}` |
| `tool_call` | Tool invoked | `{"tool": "retrieve_values", "args": {...}}` |
| `tool_result` | Tool returns | `{"tool": "retrieve_values", "result": "..."}` |
| `step_completed` | `update_step_status("stepN", "completed")` | `{"step_id": "step1"}` |
| `execution` | Agent returns `ExecutionResult` | Full structured result |
| `friendly` | Final friendly response (after validation) | `{"response": "..."}` |
| `error` | Any failure | `{"message": "..."}` |
| `done` | Pipeline complete | `{"message": "done"}` |

### Typical event timeline (from benchmark test)

```
[  0.00s] status          "Analyzing your question…"
[  4.44s] plan            Plan accepted (5 steps)
[  6.17s] step_started    step1
[  6.17s] tool_call       retrieve_values "Social security benefits"
[  6.17s] tool_result     {"2018": 332.27, "2023": 659.68}
[  8.37s] step_completed  step1
[  8.37s] step_started    step2
[  8.37s] tool_call       retrieve_values "Social assistance benefits"
[  8.37s] tool_result     {"2018": 4492.43, "2023": 13140.36}
[ 10.35s] step_completed  step2
[ 14.51s] tool_call       execute_python_code (CAGR calc)
[ 14.83s] tool_result     14.70%
[ 17.70s] step_completed  step3, step4, step5
[ 33.54s] execution       final structured result
[ 33.66s] friendly        friendly_response
```

12 of 17 events arrive before the midpoint — the user sees the plan card at
~4s, step-by-step progress from ~6s onward, and tool results as they happen.
The only wait at the end is the final synthesis (model generating
`ExecutionResult` + `friendly_response`).

---

## Context-Window Management (`clear_old_tool_results`)

A `ProcessHistory` capability runs before every model request:

- History ≤ 24 messages → untouched.
- History > 24 messages → all but the 4 most recent `ToolReturnPart` payloads
  replaced with `"[retrieve_values result cleared to save context — value already
  recorded in step_results]"`.

Tool-call/result pairing is preserved (providers require it); only payloads shrink.
Zero LLM cost. Rationale: by synthesis time, `step_results` + `final_answer` carry
everything — old raw tool outputs are dead weight.

## Observability (cross-cutting)

- `init_observability()` at import — instruments all Agents via OTel → Langfuse.
- `observe_agent_run` — trace per query, tagged with `RELEASE` env var
  (e.g. `agent-v1`).
- `observe_step` — spans for every decision: guardrail, cache, short-circuit,
  validation retries, model fallback.
- `record_usage` — tokens/cost/latency per stage + totals on the trace.

## Life of a Query

```
user query
  → guardrail screen ──✗──→ 400
  → semantic cache ────hit──→ SSE "cached" → done
  → deterministic lookup ─hit─→ direct answer (0 LLM)
  → SSE "status" → agent built (tools selected for THIS query)
  → [model request ← ProcessHistory trims old tool results]
  → write_plan ──✗──→ "PLAN REJECTED: ..." → model revises → write_plan ✓
    → SSE "plan" (UI shows plan card)
  → retrieve_values / execute_python_code
    → SSE "step_started" → "tool_call" → "tool_result" → "step_completed"
  → structured ExecutionResult
  → validate_execution_result ──✗──→ "OUTPUT REJECTED: ..." → retry (≤2)
  → SSE "execution" → friendly_response (+ disclaimer) → SSE "friendly" → "done"
  → Langfuse trace with tokens/cost/latency per stage
```

One agent. Three code-enforced gates (schema, plan semantics, output semantics).
The model only ever sees minimal, actionable correction feedback at each gate.
The user sees incremental progress via SSE throughout — never waiting blind.
