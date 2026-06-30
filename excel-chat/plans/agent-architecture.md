# Excel Analyst — Agent Architecture & System Design

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Component Map](#2-component-map)
3. [Data Flow: Upload to Query](#3-data-flow-upload-to-query)
4. [Agent Architecture](#4-agent-architecture)
5. [Tools](#5-tools)
6. [Sandbox Environment](#6-sandbox-environment)
7. [Caching System](#7-caching-system)
8. [S3 & Storage Layer](#8-s3--storage-layer)
9. [SQLite Metadata Store](#9-sqlite-metadata-store)
10. [Streaming (SSE)](#10-streaming-sse)
11. [Frontend Architecture](#11-frontend-architecture)
12. [Deployment & Docker](#12-deployment--docker)
13. [Timing & Observability](#13-timing--observability)
14. [Cleanup & Lifecycle](#14-cleanup--lifecycle)
15. [Planned: Single-Agent Refactor](#15-planned-single-agent-refactor)

---

## 1. System Overview

Excel Analyst is a financial data query application that lets users upload Excel files and ask natural-language questions about their data. The backend uses Pydantic AI agents powered by OpenRouter LLMs to classify queries, retrieve values from pandas DataFrames, perform calculations in a secure sandbox, and generate natural-language responses. The frontend is a React SPA that communicates via REST and Server-Sent Events (SSE).

**Key design principles:**
- **No vector database** — retrieval is done directly from in-memory pandas DataFrames, not embeddings/ChromaDB (disabled).
- **Multi-sheet awareness** — sheets with similar schemas are grouped for cross-sheet comparisons.
- **Layered caching** — semantic cache for full responses, result cache for intermediate retrievals/computations, exact-match LLM cache for auto-descriptions.
- **Streaming** — SSE pushes intermediate pipeline stages to the frontend so users see progress in real time.

---

## 2. Component Map

```
excel-chat/
├── backend/
│   ├── Dockerfile                  # Multi-stage: frontend build + Python backend
│   ├── requirements.txt
│   └── src/
│       ├── main.py                 # FastAPI app, endpoints, SSE, orchestration
│       ├── pipeline.py             # Pydantic AI agents, tools, pipeline orchestration
│       ├── excelservices.py        # Excel loading, cleaning, auto-description
│       ├── sheet_metadata.py       # SQLite metadata, S3 helpers, CRUD, cache tables
│       ├── semantic_cache.py       # Per-user semantic query cache (embeddings)
│       ├── cache_service.py        # Exact-match LLM cache (Redis/SQLite)
│       ├── result_cache.py         # Intermediate result cache (3 layers)
│       ├── queryservices.py        # Legacy query service (ChromaDB disabled)
│       └── classification_template.py  # Prompt template for query classification
├── frontend/
│   └── ragsheets/                  # React + Vite + TailwindCSS
│       ├── src/
│       │   ├── App.tsx             # Main layout, header, tabbed interface
│       │   └── components/ui/
│       │       ├── promptinput.tsx # Query input, SSE consumer, step display
│       │       ├── textarea.tsx
│       │       └── button.tsx
│       └── index.html
└── tests/
    └── test_optimization.py        # 28 unit tests for optimizations
```

### External Services

| Service | Purpose | Configuration |
|---------|---------|---------------|
| **OpenRouter** | LLM API for all agents | `OPENROUTER_API_KEY`, `OPENROUTER_BASE_URL` |
| **AWS S3** | Excel file storage | `S3_BUCKET` (default: `ragsheets`) |
| **Redis** (optional) | Cache backend | `REDIS_URL` — falls back to SQLite if unset |

### LLM Model

All agents use `openai/gpt-oss-120b:nitro` via OpenRouter, configurable via `MODEL_ID` env var. Selected for low latency (nitro tier) and strong structured-output compliance.

---

## 3. Data Flow: Upload to Query

### 3.1 Upload Flow

```
User selects .xlsx file
  → POST /upload/ (multipart form)
  → Save to local temp file
  → Upload to S3 (uploads/{file_id}/{filename})
  → ExcelService.load_sheet_metadata_from_file()
      → pd.read_excel(filepath, sheet_name=None)  # load all sheets
      → ExcelService.clean_dataframe() per sheet  # detect year row, set index
      → Create SheetMeta per sheet (fields, years, row_count)
  → ExcelService.detect_schema_groups()
      → Jaccard similarity on field sets, threshold 0.75
      → Assigns schema_group label ("group_0", "unique", etc.)
  → save_file() + save_sheet() to SQLite (per-user scoped)
  → ExcelService.auto_describe_all_sheets()
      → LLM generates 1-sentence description per sheet (cached)
  → Invalidate user's semantic cache (new data = old answers stale)
  → Delete local temp file
  → Return sheet metadata to frontend
```

### 3.2 Query Flow (Non-Streaming)

```
User submits query
  → GET /query?query=...  (X-User-ID header)
  → Semantic cache check (embed query, cosine similarity ≥ 0.88)
      → HIT: return cached response immediately (no LLM call)
      → MISS: continue
  → Load all SheetMeta from SQLite (user-scoped)
  → Read Excel files from S3 into memory (BytesIO, no disk I/O)
      → Cache parsed sheets per s3_key within the request
  → Build sheet context string (names, fields, years, descriptions, schema groups)
  → Format classification template with sheet context + query
  → build_query_pipeline() → run_pipeline(query)
      → Step 1: Planner agent (LLM call) → QueryPlan
      → Step 2: Pre-populate retrievals (pure Python, no LLM)
      → Step 3: Short-circuit if pure retrieve, else Executor agent (LLM call)
  → Store result in semantic cache
  → Return {answer, friendly_response, timings, cached: false}
```

### 3.3 Query Flow (Streaming)

```
User submits query
  → GET /query/stream?query=...  (SSE)
  → Same cache check + sheet loading as above
  → Pipeline runs with on_event callback
  → Events pushed to asyncio.Queue, yielded as SSE:
      event: status      → "Analyzing your question…"
      event: plan        → {task_type, plan, items, description}
      event: status      → "Retrieving data from sheets…"
      event: pre_populated → {values: {...}}
      event: status      → "Running calculations…"
      event: execution   → {step_results, final_answer, explanation}
      event: friendly    → {response: "..."}
      event: done        → {timings, total}
  → Store result in semantic cache after stream completes
```

---

## 4. Agent Architecture

The pipeline currently uses **two agents** with a deterministic short-circuit path. A third agent (responder) exists but is not used in the main flow.

### 4.1 Planner Agent (`build_planner_agent`)

**Purpose:** Classify the user's query and produce a structured execution plan.

**Model:** `openai/gpt-oss-120b:nitro`, temperature 0.1, 3 retries.

**Input:** User query string + sheet context (names, fields, years, descriptions, schema groups).

**Output:** `QueryPlan` (Pydantic model):
```python
class QueryPlan(BaseModel):
    task_type: Literal["retrieve_numbers", "perform_calculations", "give_advice", "other"]
    plan: dict[str, PlanStep] | None    # numbered steps for calculations
    items: list[str] | None             # ["FieldName, Year"] for retrievals
    description: str | None             # for advice requests
```

**System prompt includes:**
- All available sheets with their fields, years, and descriptions
- Schema group annotations (sheets sharing the same schema can be cross-compared)
- Complete enumeration of step types: `retrieve`, `retrieve_batch`, 21 named math operations, `compute`
- 10 worked examples covering simple retrieval, YoY growth, CAGR, cross-sheet comparison, batch retrieval, complex compute, and advice
- Instruction to prefer `retrieve_batch` when 2+ years are needed for the same field
- Instruction to prefer named operations over `compute` for simple math

**Key behaviors:**
- For simple lookups ("What was revenue in 2022?") → returns `task_type: "retrieve_numbers"` with `items: ["Revenue, 2022"]`
- For calculations ("What's the profit margin?") → returns `task_type: "perform_calculations"` with a multi-step `plan`
- For advice ("How to reduce costs?") → returns `task_type: "give_advice"` with a `description`

### 4.2 Executor Agent (`build_executor_agent`)

**Purpose:** Execute the plan from the planner using tools, produce structured results + friendly response.

**Model:** `openai/gpt-oss-120b:nitro`, temperature 0.1, 3 retries.

**Input:** Execution prompt built from the plan (includes pre-populated values if available).

**Output:** `ExecutionResult` (Pydantic model):
```python
class ExecutionResult(BaseModel):
    step_results: dict[str, Any]     # per-step results
    final_answer: Any                # scalar or list
    explanation: str                 # how the answer was derived
    friendly_response: str           # natural language answer for the user
```

**Tools available:** `retrieve`, `extract_val`, `retrieve_batch`, `execute_python_code` (see [§5 Tools](#5-tools)).

**System prompt includes:**
- Sheet names and count
- Detailed tool descriptions with usage examples
- Instruction to prefer `retrieve_batch` over multiple `retrieve` calls
- Sandbox capabilities (math module, builtins, statistics)
- Instruction to include a `friendly_response` field with formatted numbers

**Key behaviors:**
- Receives pre-populated values in the prompt (told "ALREADY DONE — value is X") so it doesn't re-call retrieve tools
- Handles named operations either by computing directly in Python or via `execute_python_code`
- For `compute` steps: generates Python code, calls `execute_python_code` sandbox
- Returns structured `ExecutionResult` including the natural-language `friendly_response`

### 4.3 Responder Agent (`build_responder_agent`) — *Not used in main pipeline*

**Purpose:** Generate a friendly natural-language response from calculation results.

**Status:** Bypassed. The executor agent now includes `friendly_response` in its output, making this agent redundant. It remains in the codebase for the legacy `generate_user_friendly_response()` function.

### 4.4 Hand-Off Between Agents

The hand-off is **not a conversation** — it's a **structured data pass**:

```
Planner agent
  → produces QueryPlan (Pydantic model, validated)
  → plan.model_dump() serialized to dict

Pre-population (pure Python, no LLM)
  → reads plan.plan steps
  → executes retrieve/retrieve_batch directly against DataFrames
  → returns dict[step_name → value]

Executor agent
  → receives execution prompt containing:
      - pre-populated values (as literal text)
      - remaining steps (named ops, compute) as instructions
      - original user query
  → calls tools as needed for non-pre-populated steps
  → returns ExecutionResult
```

There is no message history shared between agents. Each agent gets a fresh context.

### 4.5 Short-Circuit: Pure Retrieve

If every step in the plan is a `retrieve` or `retrieve_batch` (no compute, no named ops), the executor agent is **skipped entirely**:

```
_prepopulate_retrievals() runs all steps in pure Python
→ ExecutionResult built directly from pre-populated values
→ _format_simple_response() generates a deterministic friendly string
→ No executor LLM call needed
```

This reduces simple lookup queries to **1 LLM call** (planner only).

### 4.6 Pre-Population Optimization (`_prepopulate_retrievals`)

This function collapses N sequential LLM round-trips into a single Python pass:

1. Iterates over every step in `plan.plan`
2. For `retrieve` steps: calls `retrieve()` directly with a `SimpleNamespace` mock context
3. For `retrieve_batch` steps: calls `retrieve_batch()` directly, parses JSON result
4. For named ops / compute: skips (left for executor)
5. Returns `dict[step_name → value]`

The executor then receives these values as literals in its prompt, eliminating the need for it to call retrieve tools.

### 4.7 Step Types & Execution Lifecycle

Each step in a `QueryPlan` is a `PlanStep` with an `action` and `args` list. The execution path differs depending on the action type.

#### PlanStep Structure

```python
class PlanStep(BaseModel):
    action: Literal[
        "retrieve", "retrieve_batch", "compute",
        "add", "subtract", "multiply", "divide", "return_percentage",
        "sqrt", "power", "log", "exp", "abs", "negate",
        "max", "min", "average", "median", "stdev",
        "yoy_growth", "cagr", "ratio", "percentage_change", "difference",
    ]
    args: list[str]
```

#### Step Types by Execution Path

| Step Type | Who Executes | LLM Call? | Pre-Populated? | Example |
|-----------|-------------|-----------|----------------|---------|
| `retrieve` | Pre-population (pure Python) | No | ✅ Yes | `retrieve(["Revenue", "2022"])` → `"32500"` |
| `retrieve_batch` | Pre-population (pure Python) | No | ✅ Yes | `retrieve_batch(["Revenue", "2019", "2020", "2021"])` → `{"2019": 30000, "2020": 32000, "2021": 35000}` |
| Named ops (add, subtract, etc.) | Executor agent | Yes (1 call) | ❌ No | `add(["step1", "step2", "step3"])` → sum of 3 prior steps |
| `compute` | Executor agent → sandbox | Yes (1-2 calls) | ❌ No | `compute(["Calculate CAGR of Revenue from 2019 to 2023"])` → Python code generated + executed |

#### Execution Lifecycle for a Multi-Step Plan

Example: "Compare the growth rates of social security benefits versus social assistance benefits from 2017 to 2021"

**Planner output:**
```json
{
  "task_type": "perform_calculations",
  "plan": {
    "step1": {"action": "retrieve_batch", "args": ["Social security benefits", "2017", "2018", "2019", "2020", "2021"]},
    "step2": {"action": "retrieve_batch", "args": ["Social assistance benefits", "2017", "2018", "2019", "2020", "2021"]},
    "step3": {"action": "compute", "args": ["Calculate CAGR of Social security benefits from 2017 to 2021 using step1 values"]},
    "step4": {"action": "compute", "args": ["Calculate CAGR of Social assistance benefits from 2017 to 2021 using step2 values"]},
    "step5": {"action": "compute", "args": ["Compare step3 and step4, identify which is higher and by how much"]}
  }
}
```

**Execution flow:**

```
Step 1: Pre-population (pure Python, ~0.003s)
  ├─ step1: retrieve_batch("Social security benefits", ["2017",...,"2021"])
  │    → df.loc["Social security benefits", ["2017",...,"2021"]]
  │    → {"2017": 344.98, "2018": 420.12, "2019": 580.45, "2020": 720.89, "2021": 852.20}
  │
  ├─ step2: retrieve_batch("Social assistance benefits", ["2017",...,"2021"])
  │    → df.loc["Social assistance benefits", ["2017",...,"2021"]]
  │    → {"2017": 4099.55, "2018": 4800.12, "2019": 5600.89, "2020": 6900.45, "2021": 8792.95}
  │
  └─ step3, step4, step5: SKIPPED (compute steps — left for executor)

Step 2: Executor agent receives prompt with pre-populated values
  ├─ Prompt includes:
  │    "step1 (field: Social security benefits): ALREADY DONE — value is {"2017": 344.98, ...}"
  │    "step2 (field: Social assistance benefits): ALREADY DONE — value is {"2017": 4099.55, ...}"
  │    "Then use execute_python_code to calculate: Calculate CAGR..."
  │    "Step-to-field mapping (use these field names in friendly_response):
  │       step1 → field: Social security benefits
  │       step2 → field: Social assistance benefits"
  │
  ├─ Executor LLM call #1: Generate Python code for step3 (CAGR calc)
  │    → execute_python_code("start = 344.98; end = 852.20; years = 4; cagr = ((end/start)**(1/years)-1)*100; return cagr")
  │    → sandbox executes → "25.37"
  │
  ├─ Executor LLM call #2: Generate Python code for step4 (CAGR calc)
  │    → execute_python_code("start = 4099.55; end = 8792.95; years = 4; cagr = ((end/start)**(1/years)-1)*100; return cagr")
  │    → sandbox executes → "21.02"
  │
  └─ Executor LLM call #3: Generate step5 + friendly_response
       → step_results: {"step1": {...}, "step2": {...}, "step3": 25.37, "step4": 21.02, "step5": "Social security benefits grew faster"}
       → friendly_response: "The CAGR for Social security benefits from 2017 to 2021 is about 25.4%, while..."
```

#### How Events Map to Execution Stages

Each pipeline stage emits an SSE event so the frontend can show real-time progress:

| Pipeline Stage | SSE Event | Data | What the Frontend Shows |
|---------------|-----------|------|----------------------|
| Pipeline starts | `status` | `{"message": "Analyzing your question…"}` | Spinner with "Analyzing your question…" |
| Planner completes | `plan` | `{"task_type": "perform_calculations", "plan": {...}}` | Plan card with step-by-step breakdown; "Classified Intent" + "Execution Plan" checkmarks |
| Pre-population starts | `status` | `{"message": "Retrieving data from sheets…"}` | Spinner updates to "Retrieving data…" |
| Pre-population completes | `pre_populated` | `{"values": {"step1": {...}, "step2": {...}}}` | "Data Retrieved" checkmark; values shown in step tracker |
| Executor starts | `status` | `{"message": "Running calculations…"}` | Spinner updates to "Running calculations…" |
| Executor completes | `execution` | `{"step_results": {...}, "final_answer": ..., "explanation": "..."}` | "Calculations Complete" checkmark; step results displayed |
| Friendly response ready | `friendly` | `{"response": "The CAGR for Social security benefits…"}` | Answer card with formatted response |
| Pipeline done | `done` | `{"timings": {...}, "total": 8.01}` | "Complete" checkmark with total time |

**Short-circuit path (pure retrieve):** If all steps are `retrieve`/`retrieve_batch`, the executor is skipped. Events are:
`status` → `plan` → `status` → `pre_populated` → `status` → `friendly` → `done`

**Cache hit path:** If the semantic cache hits, no pipeline runs. Events are:
`cached` → (stream closes)

#### Step-to-Field Mapping

The executor prompt includes a step-to-field-name mapping so the executor can use actual field names in its `friendly_response`:

```
Step-to-field mapping (use these field names in your friendly_response):
  step1 → field: Social security benefits
  step2 → field: Social assistance benefits
```

This is built by iterating over the plan steps and extracting the field name from each `retrieve`/`retrieve_batch` step's args:
- `retrieve`: field is `args[0]` (or `args[1]` if `args[0]` is a sheet name)
- `retrieve_batch`: field is `args[0]` (cross-sheet) or `args[1]` (sheet-scoped)

Without this mapping, the executor would only see bare step numbers and produce generic responses like "the first series grew at 25.4%" instead of "Social security benefits grew at a CAGR of 25.4%".

---

## 5. Tools

### 5.1 `retrieve(ctx, field, year, sheet="")`

**Purpose:** Fetch a single numeric value from the DataFrame(s).

**Behavior:**
- If `sheet` is provided: searches only that sheet's DataFrame
- If `sheet` is empty: searches all sheets, returns all matches as `"SheetName: value; SheetName2: value2"`
- Returns `"ERROR: ..."` string on miss

**Caching:** Checks Layer 1 result cache before scanning DataFrame. Stores result in cache after successful retrieval.

**Schema customization:** `_prepare_retrieve_tool` injects available fields, years, and sheet names into the tool's JSON schema at runtime so the LLM sees valid options.

### 5.2 `extract_val(ctx, field, year, sheet="")`

**Purpose:** Alias for `retrieve`. Provided as a separate tool to give the LLM multiple naming options.

### 5.3 `retrieve_batch(ctx, field, years, sheet="")`

**Purpose:** Fetch multiple year values for a single field in ONE tool call.

**Behavior:**
- **Single-sheet mode** (`sheet` provided): returns JSON `{"2018": 1500.0, "2019": 1200.0, ...}`
- **Cross-sheet mode** (`sheet` empty): returns JSON `{"2018": {"Sheet1": 1500.0, "Sheet2": 1300.0}, ...}` (or scalar if only one sheet matches)
- Per-year errors return `null` for that year

**Caching:** Uses Layer 1 result cache per (field, year, sheet) tuple. Cross-sheet mode skips cache reads/writes to avoid stale values from different sheet sets.

**Why it exists:** Without this, fetching 5 years for one field required 5 separate `retrieve` calls, each a separate LLM round-trip (~2-6s each). `retrieve_batch` collapses them into one tool call.

### 5.4 `execute_python_code(ctx, code)`

**Purpose:** Execute arbitrary Python code in a secure sandbox for complex calculations.

**Behavior:**
1. Check Layer 2 sandbox cache (keyed by SHA-256 of normalized code)
2. Dedent code with `textwrap.dedent`
3. Auto-detect `result = ...` assignments and convert to `return` statements
4. Create type definitions for sandbox (math module, computed values)
5. Run via `pydantic_monty.Monty` sandbox (see [§6 Sandbox](#6-sandbox-environment))
6. Capture stdout, return output or return value
7. Cache successful results

**Sandbox capabilities:** Basic Python syntax, `math` module, common builtins (`abs`, `round`, `min`, `max`, `sum`, `len`, `sorted`), `statistics` module.

### 5.5 Named Math Operations

21 named operations are available as plan step types (not as LLM tools — they're executed deterministically):

| Category | Operations |
|----------|-----------|
| **Unary** (1 arg) | `sqrt`, `abs`, `negate`, `exp` |
| **Binary** (2 args) | `subtract`, `divide`, `return_percentage`, `power`, `log`, `yoy_growth`, `ratio`, `percentage_change`, `difference` |
| **Ternary** (3 args) | `cagr` [end_value, start_value, num_years] |
| **N-ary** (2+ args) | `add`, `multiply`, `max`, `min`, `average`, `median`, `stdev` |

Args can be step references (e.g. `"step1"`) or literal numbers (e.g. `"100"`).

These are implemented as pure Python functions in `pipeline.py` (`op_add`, `op_subtract`, etc.) and also referenced in the legacy `executing_plan_from_json` function.

---

## 6. Sandbox Environment

The sandbox uses **pydantic-monty** (`Monty` class) to execute LLM-generated Python code safely. This is critical for production reliability — the sandbox must execute arbitrary LLM-generated code without crashing the server, accessing the filesystem, or making network calls.

### 6.1 What is pydantic-monty?

`pydantic-monty` is a lightweight Python sandbox library that interprets Python code in a restricted namespace. It does **not** use `exec()` or `eval()` — it parses the code into an AST and evaluates it with controlled scope. This means:

- **No `import` statement** — the LLM cannot import arbitrary modules
- **No file I/O** — `open()`, `os`, `subprocess` are not available
- **No network access** — `socket`, `urllib`, `requests` are not available
- **No global state mutation** — the sandbox runs in an isolated namespace

### 6.2 Code Processing Pipeline

When the executor agent calls `execute_python_code(code)`, the following steps occur:

```
LLM generates code string
  │
  ├─ Layer 2 cache check (SHA-256 of normalized code)
  │    HIT → return cached output immediately
  │    MISS → continue
  │
  ├─ 1. Dedent: textwrap.dedent(code).strip()
  │    Removes common leading whitespace from multi-line strings.
  │    LLMs often generate code inside triple-quoted strings with
  │    indentation that pydantic-monty rejects ("Unexpected indentation").
  │
  ├─ 2. Return-statement injection
  │    If code doesn't start with `return`, check for assignments to
  │    `result` or `result_` variables. Convert the last assignment to
  │    a `return` statement so the sandbox produces a return value.
  │    Example: "result = a + b" → "return a + b"
  │
  ├─ 3. Type definitions injection
  │    Pre-inject stubs for the sandbox's type checker:
  │    - `import math` (available in sandbox)
  │    - `from typing import Any`
  │    - Computed values from pipeline: `step1: float = 0.0` etc.
  │    These are type stubs, not runtime values — they tell Monty's
  │    type checker what types exist so it doesn't reject the code.
  │
  ├─ 4. Monty instance creation
  │    m = pydantic_monty.Monty(
  │        code,
  │        inputs=[],
  │        script_name="sandbox.py",
  │        type_check=False,         # disable strict type checking
  │        type_check_stubs=type_defs,
  │    )
  │
  ├─ 5. stdout capture
  │    sys.stdout redirected to io.StringIO() so any print() output
  │    is captured and returned as the result if no return statement
  │    produces a value.
  │
  ├─ 6. Execution: await m.run_async(inputs={}, external_functions={})
  │    Runs the code asynchronously in the sandbox.
  │    Returns the value of the `return` statement, or None.
  │
  ├─ 7. Result extraction
  │    - If return value is not None → str(output)
  │    - Elif stdout has content → stdout_output.strip()
  │    - Else → "Code executed successfully (no output)"
  │
  ├─ 8. Cache store: sandbox_cache_set(user_id, code, result)
  │    Store successful result for future cache hits.
  │
  └─ 9. stdout restore: sys.stdout = old_stdout (in finally block)
```

### 6.3 What's Available in the Sandbox

The sandbox has access to a deliberately limited set of Python capabilities:

| Category | Available | NOT Available |
|----------|-----------|---------------|
| **Syntax** | Variables, arithmetic (`+`, `-`, `*`, `/`, `**`, `//`, `%`), conditionals (`if/else`), loops (`for`, `while`), list/dict comprehensions, f-strings | `import`, `class`, decorators, `yield`, `async/await` |
| **Modules** | `math` (pre-injected: `math.sqrt`, `math.pow`, `math.log`, `math.exp`, `math.ceil`, `math.floor`, etc.) | `os`, `sys`, `subprocess`, `socket`, `urllib`, `json`, `pickle`, `open()` |
| **Builtins** | `abs`, `round`, `min`, `max`, `sum`, `len`, `sorted`, `range`, `int`, `float`, `str`, `list`, `dict`, `tuple`, `set`, `bool`, `enumerate`, `zip`, `map`, `filter` | `exec`, `eval`, `compile`, `globals`, `locals`, `__import__`, `getattr` (on dangerous objects) |
| **Statistics** | `statistics` module (mean, median, stdev, variance) — available if injected | `numpy`, `pandas`, `scipy` (not injected) |
| **External functions** | Empty dict `{}` — no external functions are passed in | Any function from the host application |

### 6.4 Type Definitions (Stubs)

The sandbox receives type stubs that declare what variables and modules exist. These are **type annotations**, not runtime values — they tell Monty's internal type checker what's valid so it doesn't reject code that references pre-populated values.

```python
type_defs = """
import math
from typing import Any

# Computed values from the pipeline will be injected
step1: float = 0.0
step2: float = 0.0
...
"""
```

For each computed value from `ctx.deps.computed_values`:
- If the value is `int` or `float` → declare as `float` type
- Otherwise → declare as `Any` type

`type_check=False` is set on the Monty instance, which disables strict type enforcement. This is important because the LLM-generated code may use dynamic typing patterns that strict checkers would reject.

### 6.5 Production Reliability Considerations

**Error handling:** The entire sandbox execution is wrapped in a try/except. On any error (syntax error, runtime error, timeout), the function returns an error string rather than crashing:

```python
try:
    # ... sandbox execution ...
    return result
finally:
    sys.stdout = old_stdout  # always restore stdout
```

If an exception occurs, it's caught and returned as:
```python
return f"Error executing code: {type(e).__name__}: {e}"
```

This means the executor agent receives an error string as the tool result and can decide how to handle it (retry with different code, report the error to the user, etc.).

**stdout restoration:** The `sys.stdout` redirect is in a `finally` block, so even if the sandbox crashes, stdout is always restored to the original value. Without this, a sandbox crash would leave the server's stdout pointing at a `StringIO` buffer, breaking all subsequent logging.

**No timeout:** Currently, the sandbox does not enforce a timeout on code execution. The `Monty.run_async()` call awaits completion. For production, a timeout should be added (e.g., `asyncio.wait_for(m.run_async(...), timeout=10)`) to prevent infinite loops in LLM-generated code. This is a planned improvement.

**No memory limits:** The sandbox runs in the same process as the FastAPI server. A `while True` loop with list appends could consume all available memory. For production hardening, consider:
1. Running sandbox code in a subprocess with `resource.setrlimit` (CPU and memory limits)
2. Using `asyncio.wait_for` with a timeout
3. Adding a statement count limit (reject code with > 100 statements)

**Caching:** Layer 2 sandbox cache — keyed by SHA-256 of normalized code (comments stripped, whitespace normalized). Same code = same result, skip execution. This means if the LLM generates identical code for a similar computation in a future query, the sandbox doesn't even run.

### 6.6 Code Normalization for Caching

Before hashing the code for cache lookup, the code is dedented and stripped. However, the current implementation does **not** normalize variable names, strip comments, or sort declarations. This means:

- `revenue = 1500; expenses = 800; return (revenue - expenses) / revenue * 100` and
- `r = 1500; e = 800; return (r - e) / r * 100`

produce different cache keys despite computing the same result. This is a known limitation — the cache hit rate could be improved by normalizing variable names and stripping comments before hashing. For now, the semantic cache (full query response) catches paraphrased queries that produce different code.

### 6.7 Typical Code Generated by the Executor

The executor agent generates Python code based on the computation step description and pre-populated values. Examples:

**Simple arithmetic:**
```python
revenue_2022 = 1500000
expenses_2022 = 800000
margin = (revenue_2022 - expenses_2022) / revenue_2022 * 100
return margin
```

**Statistical calculation (stdev):**
```python
import statistics
values = [69871.69, 72123.45, 75000.12, 78000.89, 82000.34,
          85000.56, 88000.78, 91000.12, 94000.45, 97000.89]
return statistics.stdev(values)
```

**CAGR calculation:**
```python
start_value = 344.98
end_value = 852.20
num_years = 4
cagr = ((end_value / start_value) ** (1 / num_years) - 1) * 100
return cagr
```

**Year-over-year differences:**
```python
values = {"2019": 35142.82, "2020": 34393.4, "2021": 35822.3, "2022": 39624.019, "2023": 47590.93}
years = sorted(values.keys())
diffs = {}
for i in range(1, len(years)):
    diffs[years[i]] = values[years[i]] - values[years[i-1]]
max_year = max(diffs, key=diffs.get)
return {"differences": diffs, "max_year": max_year, "max_increase": diffs[max_year]}
```

---

## 7. Caching System

The system has **four distinct caching layers**, each serving a different purpose:

### 7.1 Semantic Cache (Full Response Cache)

**File:** `semantic_cache.py`

**Purpose:** Cache entire query responses by semantic similarity so paraphrased queries hit the same cache entry.

**How it works:**
1. User query is embedded using `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~80MB)
2. Cosine similarity computed against all cached embeddings for that user
3. If similarity ≥ 0.88, return cached response without invoking the pipeline
4. On miss, after pipeline completes, store the response + query embedding

**Storage:**
- **Redis** (primary): `FT.SEARCH` KNN vector query when `REDIS_URL` is set
- **SQLite** (fallback): `embedding_json` column on `llm_cache` table, NumPy cosine similarity in Python

**User isolation:** All lookups scoped by `user_id`. User A's "revenue" query never returns User B's cached answer.

**Invalidation:** On file upload or delete, `invalidate_user_cache(user_id)` deletes all entries for that user.

**Model:** `all-MiniLM-L6-v2` — chosen over `Qwen/Qwen3-Embedding-8B` (16GB, failed to load) for its small size (80MB) and fast inference. Pre-downloaded in Dockerfile to avoid cold-start latency.

**Threshold:** 0.88 — high enough to avoid false positives, low enough to catch paraphrases like "Revenue in 2022" vs "2022 revenue".

### 7.2 Exact-Match LLM Cache

**File:** `cache_service.py`

**Purpose:** Cache LLM responses by exact (model, prompt) hash. Used for auto-description generation where the same sheet profile produces the same description.

**Key:** SHA-256 of `f"{model}:{prompt}"`

**Storage:**
- **Redis** (primary): `llm:{hash}` key with 7-day TTL, sliding expiration on hit
- **SQLite** (fallback): `llm_cache` table

**Usage:** Called by `ExcelService.auto_describe_sheet()` to avoid regenerating descriptions for identical sheet profiles.

### 7.3 Result Cache (Intermediate Results)

**File:** `result_cache.py`

**Purpose:** Cache individual retrieval and computation results so subsequent queries reuse them without re-scanning DataFrames or re-executing sandbox code.

**Three layers:**

#### Layer 1: Retrieve Cache
- **Key:** `build_retrieve_key(field, year, sheet)` → e.g. `revenue_2022` or `sheetA.revenue_2022`
- **What it caches:** Raw tool return strings from `retrieve()` and `retrieve_batch()`
- **When it's checked:** At the start of every `retrieve()` call, before scanning the DataFrame
- **When it's written:** After every successful `retrieve()` call
- **Per-user scoped:** Yes, via `user_id` prefix

#### Layer 2: Sandbox Cache
- **Key:** SHA-256 of normalized Python code (comments stripped, whitespace normalized)
- **What it caches:** Output string from `execute_python_code()`
- **When it's checked:** At the start of every `execute_python_code()` call
- **When it's written:** After every successful sandbox execution
- **Per-user scoped:** Yes

#### Layer 3: Post-Execution Structured Keys
- **Key:** Canonical structured key derived from plan step + args, e.g. `sum_revenue_2022_revenue_2023`
- **What it caches:** Computed values from named operations and compute steps
- **When it's written:** After executor completes, `cache_step_results()` iterates over plan steps and stores each step result under its canonical key
- **Semantic alias:** For `compute` steps, the natural-language description is embedded and stored alongside the structured key, enabling `find_similar_step()` to locate similar compute results

**Normalization helpers:**
- `normalize_field("Wages and salaries")` → `"wages_and_salaries"`
- `normalize_year("2022")` → `"2022"` (preserves formats like FY2022)
- `normalize_sheet("Balance Sheet")` → `"balance_sheet"`
- `normalize_operation("total")` → `"sum"` (via alias map)

**Storage:** Redis (primary) or SQLite `result_cache` table (fallback). 7-day TTL.

**Invalidation:** `invalidate_user_results(user_id)` deletes all result cache entries for a user. Called on file upload/delete.

### 7.4 Cache Invalidation Triggers

| Event | What's invalidated | Function |
|-------|-------------------|----------|
| File upload | Semantic cache for user | `semantic_invalidate_user_cache(user_id)` |
| File delete | Semantic cache for user | `semantic_invalidate_user_cache(user_id)` |
| Daily cleanup (3 AM) | Stale LLM cache entries (>7 days not accessed) | `cleanup_old_cache_entries(7)` |
| Daily cleanup (3 AM) | Files older than 90 days | `delete_files_older_than(90)` |

---

## 8. S3 & Storage Layer

**File:** `sheet_metadata.py` (S3 helpers), `main.py` (usage)

### S3 Operations

| Function | Purpose |
|----------|---------|
| `upload_to_s3(filepath, s3_key)` | Upload Excel file to S3 during upload |
| `read_excel_from_s3(s3_key)` | Stream Excel from S3 into BytesIO, parse all sheets in memory |
| `delete_from_s3(s3_key)` | Delete file from S3 during file deletion |

### In-Memory Read Optimization

`read_excel_from_s3()` replaces the old `download_from_s3()` + `pandas.read_excel(filepath)` pattern:

```
OLD: S3 GET → write to disk → pandas.read_excel(filepath)     ~2-3s overhead
NEW: S3 GET → BytesIO buffer → pandas.read_excel(buffer)      ~0.5s overhead
```

Within a single query request, parsed sheets are cached per `s3_key` so multiple `SheetMeta` entries pointing at the same file only fetch + parse once.

### S3 Lifecycle

- S3 bucket has a lifecycle policy: `uploads/*` objects expire after 90 days
- The daily cleanup cron removes SQLite metadata for files whose S3 objects have expired
- `touch_file_access(file_id)` updates `last_accessed` on every query to track active files

---

## 9. SQLite Metadata Store

**File:** `sheet_metadata.py`

**Database:** `sheets.db` (SQLite, stored at `DATA_DIR/sheets.db`, default: `backend/src/sheets.db`)

### 9.1 Role: Index & Catalog Layer (Not a Query Engine)

SQLite serves as the **index/catalog layer** for the application. It tracks *what data exists*, *where it lives in S3*, and *what the schema looks like* — but it **never executes user queries**. No SQL `SELECT ... WHERE` is ever run to answer a financial question.

**What SQLite does:**
- Catalogs uploaded files and their sheets (metadata only — field names, year columns, schema groups, descriptions)
- Stores cached LLM responses and intermediate pipeline results
- Tracks file access timestamps for lifecycle management

**What SQLite does NOT do:**
- Store financial data values (those live in the Excel files in S3, loaded into pandas DataFrames at query time)
- Execute analytical queries (no `SUM`, `AVG`, `GROUP BY` — all math is done in Python via pandas/numpy/sandbox)
- Serve as a data warehouse (it's a bookkeeping layer, not an OLAP engine)

The query path is entirely:

```
S3 → BytesIO → pandas DataFrame → df.loc[field, year] → Python computation
```

SQLite tells the app *which* DataFrames to load and *what fields/years* they contain, but the actual data retrieval and computation happens in-memory with pandas.

### 9.2 When SQLite Is Accessed in the Pipeline

The following diagram shows every point where SQLite is read or written during a query:

```
User submits query
  │
  ├─ READ: Semantic cache lookup
  │    list_user_embeddings(user_id) → loads all embeddings + responses
  │    NumPy cosine similarity computed in Python (not SQL)
  │    HIT → return cached response (pipeline ends here)
  │    MISS → continue
  │
  ├─ READ: Sheet metadata loading
  │    get_all_sheets(user_id) → SELECT from sheets WHERE user_id=?
  │    Returns list[SheetMeta] with fields, years, schema_group, descriptions
  │    This tells the app which S3 keys to fetch and what schema to expect
  │
  ├─ (pandas DataFrames loaded from S3 — no SQLite involved)
  │
  ├─ READ/WRITE: Result cache (Layer 1 — retrieve)
  │    result_cache_get(user_id, key) → SELECT from result_cache
  │    result_cache_set(user_id, key, value) → INSERT/REPLACE into result_cache
  │    Called inside retrieve() and retrieve_batch() tools
  │
  ├─ READ/WRITE: Result cache (Layer 2 — sandbox)
  │    result_cache_get/set with cache_type='sandbox'
  │    Called inside execute_python_code() tool
  │
  ├─ WRITE: Result cache (Layer 3 — post-execution)
  │    cache_step_results(user_id, plan, execution)
  │    Stores each step's result under a canonical structured key
  │    Called after executor agent completes
  │
  ├─ WRITE: Semantic cache store
  │    set_cached_response(model, query, response, user_id, embedding)
  │    INSERT/REPLACE into llm_cache with embedding_json
  │    Called after pipeline completes successfully
  │
  └─ WRITE: File access tracking
       touch_file_access(file_id) → UPDATE files SET last_accessed=now
       Called for each file whose sheets were loaded
```

**Key insight:** SQLite is accessed at the *edges* of the pipeline (before and after the LLM agents run), not *during* agent reasoning. The planner and executor agents never query SQLite directly — they operate on pandas DataFrames and pre-populated values. The only connection between SQLite and the agents is:

1. **Before agents run:** `get_all_sheets()` populates the planner's system prompt with available fields/years
2. **During tool calls:** `result_cache_get/set` transparently caches retrieve and sandbox results (agents don't know this is happening)
3. **After agents complete:** `cache_step_results()` stores structured results for future queries

### 9.3 Tables & Schema

#### `files` — Uploaded File Catalog

| Column | Type | Description |
|--------|------|-------------|
| `file_id` | TEXT PK | UUID |
| `file_name` | TEXT | Original filename (e.g. "budget_2023.xlsx") |
| `s3_key` | TEXT | S3 object key (e.g. "uploads/{uuid}/budget_2023.xlsx") |
| `sheet_count` | INTEGER | Number of sheets in the file |
| `created_at` | TEXT | Upload timestamp (`datetime('now')`) |
| `last_accessed` | TEXT | Last query timestamp (updated via `touch_file_access`) |
| `user_id` | TEXT | Owner (default: `anonymous`) |

**Purpose:** Records *which files exist* and *where they live in S3*. The `s3_key` is the bridge between SQLite metadata and the actual Excel data — at query time, `read_excel_from_s3(s3_key)` fetches the file content.

**Indexes:** None beyond the primary key. Lookups are by `user_id` (small result sets).

#### `sheets` — Sheet Schema Catalog

| Column | Type | Description |
|--------|------|-------------|
| `sheet_id` | TEXT PK | UUID |
| `file_id` | TEXT FK | Parent file (`FOREIGN KEY ... ON DELETE CASCADE`) |
| `file_name` | TEXT | Denormalized parent filename |
| `sheet_name` | TEXT | Tab name in the Excel file (e.g. "Revenue", "Balance Sheet") |
| `s3_key` | TEXT | S3 object key (shared with parent file) |
| `fields_json` | TEXT | JSON array of row index labels — the financial line items (e.g. `["Revenue", "Expenses", "Net Income"]`) |
| `years_json` | TEXT | JSON array of column headers — the fiscal years (e.g. `["2018", "2019", "2020", "2021"]`) |
| `schema_group` | TEXT | Group label for cross-sheet comparison (e.g. `"group_0"`, `"unique"`) |
| `user_description` | TEXT | User-provided description (optional) |
| `auto_description` | TEXT | LLM-generated 1-sentence description |
| `row_count` | INTEGER | Number of data rows |
| `created_at` | TEXT | Creation timestamp |
| `user_id` | TEXT | Owner |

**Purpose:** This is the **schema catalog**. It tells the planner agent what fields and years are available without loading the Excel file. The planner's system prompt is built from this data:

```
Available sheets:
  Sheet "Revenue" (file: budget_2023.xlsx)
    Fields: Revenue, Cost of Goods Sold, Gross Profit, Operating Expenses, Net Income
    Years: 2018, 2019, 2020, 2021, 2022, 2023
    Description: Annual revenue and expense breakdown
    Schema group: group_0
```

This means the planner can make intelligent decisions about which steps to plan *without* the DataFrame being loaded yet. The DataFrame is only loaded after the plan is created, during pre-population or executor tool calls.

**How `fields_json` and `years_json` are populated:** During upload, `ExcelService.load_sheet_metadata_from_file()` reads the Excel file with `pandas.read_excel(sheet_name=None)`, cleans each sheet (detects year row, sets index), then extracts `df.index.tolist()` (fields) and `df.columns.tolist()` (years). These are JSON-serialized and stored.

#### `llm_cache` — Semantic & Exact-Match Response Cache

| Column | Type | Description |
|--------|------|-------------|
| `cache_key` | TEXT PK | SHA-256 of `f"{model}:{prompt}"` |
| `response` | TEXT | Cached LLM response (full JSON or text) |
| `model` | TEXT | Model name (e.g. `openai/gpt-oss-120b:nitro`) |
| `created_at` | TEXT | First stored timestamp |
| `last_accessed` | TEXT | Last cache hit timestamp (for TTL cleanup) |
| `hit_count` | INTEGER | Number of cache hits |
| `user_id` | TEXT | Owner |
| `embedding_json` | TEXT | 384-dim embedding (JSON array) for semantic similarity search |

**Purpose:** Serves **two caching roles** depending on whether `embedding_json` is populated:

1. **Exact-match LLM cache** (no embedding): Used by `cache_service.py` for auto-description generation. Key is `SHA-256(model:prompt)`. If the same sheet profile is uploaded again, the description is served from cache without an LLM call.

2. **Semantic response cache** (with embedding): Used by `semantic_cache.py` for full query response caching. The user's query is embedded with `all-MiniLM-L6-v2` (384-dim), and cosine similarity is computed against all `embedding_json` values for that user. If similarity ≥ 0.88, the cached `response` is returned without running the pipeline.

**Indexes:**
- `idx_llm_cache_last_accessed` on `last_accessed` — used by daily cleanup cron
- `idx_llm_cache_user_id` on `user_id` — used by semantic cache lookup

**How semantic similarity works (SQLite fallback):**
When Redis is unavailable, `list_user_embeddings(user_id)` loads all `(cache_key, response, embedding_json)` tuples for the user. Cosine similarity is computed in NumPy against each embedding — a brute-force scan. This works up to ~10K entries per user; Redis Stack's `FT.SEARCH` KNN query is recommended for scale.

#### `result_cache` — Intermediate Pipeline Result Cache

| Column | Type | Description |
|--------|------|-------------|
| `cache_key` | TEXT | Structured key (e.g. `revenue_2022`) or SHA-256 of code |
| `user_id` | TEXT | Owner |
| `value` | TEXT | Cached result string |
| `cache_type` | TEXT | `"retrieve"`, `"sandbox"`, `"structured"`, or `"semantic_step"` |
| `structured_key` | TEXT | Canonical normalized key for structured lookups |
| `description` | TEXT | Natural-language description (for compute steps) |
| `embedding_json` | TEXT | Embedding of description (for semantic step search) |
| `created_at` | TEXT | First stored timestamp |
| `last_accessed` | TEXT | Last hit timestamp |
| `hit_count` | INTEGER | Number of cache hits |

**Primary key:** Composite `(cache_key, user_id)` — same key can exist for different users.

**Purpose:** Caches **intermediate results** from within the pipeline so subsequent queries can skip re-scanning DataFrames or re-executing sandbox code. This is distinct from the semantic cache (which caches entire responses).

**Three cache layers stored here:**

| `cache_type` | What it caches | Key format | When it's checked |
|--------------|---------------|------------|-------------------|
| `retrieve` | Raw `df.loc[field, year]` values | `field_year` or `sheet.field_year` | Start of every `retrieve()` / `retrieve_batch()` call |
| `sandbox` | `execute_python_code()` output | SHA-256 of normalized code | Start of every `execute_python_code()` call |
| `structured` | Named operation / compute step results | Canonical key (e.g. `sum_revenue_2022_revenue_2023`) | Written after executor completes; read by future queries with same step pattern |
| `semantic_step` | Compute step with embedding for fuzzy matching | Same as structured + embedding | Written alongside structured; enables `find_similar_step()` for similar compute descriptions |

**Indexes:**
- `idx_result_cache_user` on `user_id`
- `idx_result_cache_user_type` on `(user_id, cache_type)`

**How it connects to agent tool calls:** The `retrieve()` tool function in `pipeline.py` calls `result_cache_get()` *before* scanning the DataFrame. If the cache has a value for `(user_id, "revenue_2022")`, it returns immediately without touching the DataFrame. The agent doesn't know this is happening — the cache is transparent to the LLM. Similarly, `execute_python_code()` checks the sandbox cache before running the Monty sandbox.

### 9.4 Schema Migrations

`init_db()` runs on application startup and performs idempotent schema setup:

1. **Creates tables** if they don't exist (`files`, `sheets`, `llm_cache`, `result_cache`)
2. **Adds columns** to pre-existing tables via `ALTER TABLE` (guarded by `PRAGMA table_info` checks):
   - `files.last_accessed`, `files.user_id`
   - `sheets.user_id`
   - `llm_cache.user_id`, `llm_cache.embedding_json`
3. **Creates indexes** after migrations complete (indexes referencing migrated columns must come after `ALTER TABLE`)

All migrations are idempotent — safe to run on both fresh and existing databases.

### 9.5 Data Models (Python Side)

**`SheetMeta`** (dataclass):
- `sheet_id`, `file_id`, `file_name`, `sheet_name`, `s3_key`
- `fields: list[str]` — row index labels (financial line items)
- `years: list[str]` — column headers (fiscal years)
- `schema_group: str` — group label for cross-sheet comparison
- `user_description: str` — user-provided
- `auto_description: str` — LLM-generated
- `combined_description` property — concatenates both
- `to_dict()` — serializes for API responses

**`FileMeta`** (dataclass):
- `file_id`, `file_name`, `s3_key`, `sheet_count`, `created_at`, `last_accessed`

These dataclasses are populated from SQLite rows via `_row_to_sheetmeta()` and `_row_to_filemeta()` helpers. They are passed throughout the pipeline — the planner agent receives them to build its system prompt, and the executor agent receives them via `PipelineDeps`.

### 9.6 SQLite in the Application Ecosystem

```
┌──────────────────────────────────────────────────────────────────┐
│                     Application Ecosystem                         │
│                                                                  │
│  ┌─────────┐    ┌──────────┐    ┌─────────────────────────────┐ │
│  │  S3     │    │ SQLite   │    │  In-Memory (per request)    │ │
│  │  (data) │    │ (catalog)│    │  pandas DataFrames          │ │
│  │         │    │          │    │                             │ │
│  │ Excel   │    │ files    │    │  df.loc[field, year]        │ │
│  │ files   │    │ sheets   │    │  ↓                           │ │
│  │ (.xlsx) │    │ llm_cache│    │  Python computation         │ │
│  │         │    │ result_  │    │  (math/statistics/sandbox)  │ │
│  │         │    │ cache    │    │                             │ │
│  └────┬────┘    └────┬─────┘    └──────────┬──────────────────┘ │
│       │              │                     │                     │
│       │  s3_key      │  SheetMeta          │  values             │
│       │  (read at    │  (read at start     │  (computed at       │
│       │   query time)│   of pipeline)      │   query time)       │
│       │              │                     │                     │
│       ▼              ▼                     ▼                     │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              FastAPI Backend (main.py)                    │   │
│  │                                                           │   │
│  │  1. get_all_sheets(user_id) → SQLite READ                 │   │
│  │  2. read_excel_from_s3(s3_key) → S3 READ → pandas         │   │
│  │  3. build_query_pipeline(sheets, sheet_metas)             │   │
│  │  4. Pipeline runs (planner → pre-populate → executor)     │   │
│  │     - Tools transparently use result_cache (SQLite R/W)   │   │
│  │  5. set_cached_response() → SQLite WRITE (semantic cache) │   │
│  │  6. touch_file_access() → SQLite WRITE (access tracking)  │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

### 9.7 SQLite vs. Redis: Division of Labor

SQLite is the **source of truth**. Redis is an optional **acceleration layer**.

| Concern | SQLite (always available) | Redis (optional, when `REDIS_URL` set) |
|---------|--------------------------|---------------------------------------|
| File/sheet metadata | ✅ Primary store | ❌ Not cached |
| Semantic cache (embeddings) | ✅ `embedding_json` column, NumPy cosine sim | ✅ `FT.SEARCH` KNN vector query (faster) |
| Exact-match LLM cache | ✅ `llm_cache` table | ✅ `llm:{hash}` key with TTL |
| Result cache (retrieve/sandbox) | ✅ `result_cache` table | ✅ `result:{user}:{key}` with TTL |
| Cache invalidation | ✅ `DELETE WHERE user_id=?` | ✅ Pattern-based key deletion |

When Redis is configured, both stores are written (SQLite as source of truth, Redis for fast lookups). On reads, Redis is checked first; on miss, SQLite is checked. If Redis is unavailable, the system degrades gracefully to SQLite-only.

### 9.8 Connection Management

SQLite connections are opened per-operation via `_get_db()`:

```python
def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn
```

Each CRUD function opens a connection, executes its query, commits, and closes. This is safe for the current single-process deployment. For multi-worker deployments (e.g., `uvicorn --workers 4`), SQLite's WAL mode should be enabled to avoid write contention, or Redis should be used as the primary cache backend.

### 9.9 Lifecycle & Cleanup

| Operation | Trigger | What happens in SQLite |
|-----------|---------|----------------------|
| File upload | `POST /upload/` | `save_file()` + `save_sheet()` inserts; `invalidate_user_cache()` deletes stale cache |
| File delete | `DELETE /files/{id}` | `delete_file()` deletes file row (cascades to sheets); `delete_from_s3()` removes S3 object; `invalidate_user_cache()` clears cache |
| Query | `GET /query` | `get_all_sheets()` read; `result_cache_get/set` during tools; `set_cached_response()` after pipeline; `touch_file_access()` update |
| Daily cleanup (3 AM) | APScheduler cron | `delete_files_older_than(90)` removes stale file metadata; `cleanup_old_cache_entries(7)` purges old cache entries |

---

## 10. Streaming (SSE)

### Backend (`main.py`)

**Endpoint:** `GET /query/stream?query=...`

**Protocol:** Server-Sent Events (SSE) via FastAPI `StreamingResponse` with `media_type="text/event-stream"`.

**Implementation:**
1. An `asyncio.Queue` is created as the event channel
2. An `on_event(event_type, data)` callback is passed to `build_query_pipeline()`
3. The callback calls `event_queue.put_nowait((event_type, json_payload))`
4. The pipeline runs as a background `asyncio.Task`
5. The stream generator awaits events from the queue with a 100ms timeout, yielding them as SSE-formatted strings
6. When the pipeline task completes and the queue is empty, the stream ends
7. The result is stored in the semantic cache after streaming completes

**SSE Event Types:**

| Event | Data | When |
|-------|------|------|
| `status` | `{"message": "..."}` | At each pipeline stage transition |
| `plan` | `{"task_type": "...", "plan": {...}, "items": [...], "description": "..."}` | After planner agent completes |
| `pre_populated` | `{"values": {...}}` | After pre-population runs |
| `execution` | `{"step_results": {...}, "final_answer": ..., "explanation": "..."}` | After executor agent completes |
| `friendly` | `{"response": "..."}` | When friendly response is generated |
| `done` | `{"timings": {...}, "total": ...}` | Pipeline complete |
| `cached` | `{"answer": ..., "friendly_response": ..., "similarity": ...}` | Semantic cache hit |
| `error` | `{"message": "..."}` | Any error during pipeline |

**Headers:** `Cache-Control: no-cache`, `Connection: keep-alive`, `X-Accel-Buffering: no` (disables proxy buffering).

### Frontend (`promptinput.tsx`)

**Implementation:** Uses `EventSource` API to consume SSE.

**UI rendering:**
1. **Plan card** — displays task type + step-by-step plan (action + args) as soon as the `plan` event arrives
2. **Progress tracker** — checkmarked steps as they complete:
   - "Classified Intent" → "Execution Plan" → "Data Retrieved" → "Calculations Complete" → "Complete"
3. **Answer card** — final LLM response (blue-highlighted card)
4. **Calculation Results** — step results with values
5. **Status indicator** — inline spinner with current status message (replaces old full-screen blur)

**Event handling:**
- `status` → updates status message
- `plan` → sets plan data, adds "Classified Intent" and "Execution Plan" steps
- `pre_populated` → adds "Data Retrieved" step
- `execution` → adds "Calculations Complete" step, sets answer data
- `friendly` → sets friendly response
- `cached` → sets answer + response, adds "Cache Hit" step
- `done` → adds "Complete" step with total time, closes EventSource
- `error` → sets error message, closes EventSource

---

## 11. Frontend Architecture

**Stack:** React + Vite + TailwindCSS + shadcn/ui components + Lucide icons

**Key files:**
- `App.tsx` — main layout with header ("Excel Analyst") and tabbed interface (Upload / Query)
- `promptinput.tsx` — query input, SSE consumer, step/plan/answer rendering
- `textarea.tsx` — styled textarea component
- `button.tsx` — styled button component

**Environment:** `VITE_API_ENDPOINT` — backend API URL (e.g. `http://localhost:8000/api`)

**Build:** `npm run build` → outputs to `dist/` → copied into Docker image at `/app/static/`

---

## 12. Deployment & Docker

### Dockerfile (Multi-Stage)

**Stage 1: Frontend Build**
- Base: `node:22-bookworm-slim`
- Copies `package.json` + `package-lock.json`, runs `npm install`
- Copies frontend source, runs `npm run build`
- Output: `/frontend/dist/`

**Stage 2: Python Backend**
- Base: `python:3.12.12-slim-bookworm`
- Installs `build-essential` and `libffi-dev` for C extensions
- Copies `requirements.txt`, runs `pip install`
- Pre-downloads `all-MiniLM-L6-v2` model to avoid cold-start latency
- Copies backend source
- Copies frontend build from Stage 1 to `/app/static/`
- Runs `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`

### Static File Serving

The backend serves the built frontend from `/app/static/` using FastAPI's `StaticFiles` mount. All API routes are under the `/api` root path (`root_path='/api'`).

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENROUTER_API_KEY` | — | Required: LLM API key |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | LLM API endpoint |
| `MODEL_ID` | `openai/gpt-oss-120b:nitro` | LLM model for all agents |
| `S3_BUCKET` | `ragsheets` | S3 bucket name |
| `REDIS_URL` | — | Optional: Redis for caching |
| `CORS_ORIGINS` | `http://localhost:5173,...` | Allowed CORS origins |
| `PORT` | `8000` | Server port |
| `DATA_DIR` | `backend/src/` | SQLite DB location |

---

## 13. Timing & Observability

### `timed()` Context Manager

**File:** `pipeline.py`

```python
@contextmanager
def timed(stage: str, timings: dict[str, float]):
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        timings[stage] = timings.get(stage, 0.0) + elapsed
        print(f"⏱️  {stage}: {elapsed:.2f}s")
```

### Stages Tracked

| Stage | Where | What it measures |
|-------|-------|-----------------|
| `semantic_cache_lookup` | `main.py` | Embedding + similarity search |
| `s3_load` | `main.py` | S3 read + Excel parse for all sheets |
| `planner` | `pipeline.py` | Planner agent LLM call |
| `pre_populate` | `pipeline.py` | Pure Python retrieval pass |
| `executor` | `pipeline.py` | Executor agent LLM call |
| `cache_write` | `main.py` | Semantic cache store |
| `pipeline` | `main.py` | Total pipeline time (wraps all above) |

Timings are returned in the API response and surfaced in SSE `done` events. Total is printed to stdout.

### Logging

All stages print emoji-prefixed log lines:
- `📋 Plan:` — planner output (JSON)
- `⚡ Pre-populated N values:` — pre-population results
- `⚡ Skipped executor` — pure retrieve short-circuit
- `✅ Execution:` — executor output (JSON)
- `📝 Response:` — friendly response (truncated)
- `⏱️  Pipeline total:` — timing summary
- `⚠️` — warnings (cache failures, S3 errors, etc.)

---

## 14. Cleanup & Lifecycle

### Daily Cleanup Cron

**File:** `main.py`

**Scheduler:** APScheduler `AsyncIOScheduler`, runs at 3:00 AM daily.

**What it does:**
1. `delete_files_older_than(90)` — deletes SQLite metadata for files older than 90 days (S3 lifecycle will have already removed the objects)
2. `cleanup_old_cache_entries(7)` — purges LLM cache entries not accessed in 7 days (SQLite fallback only; Redis TTL handles this natively)

### Stale File Detection

`find_stale_files()` buckets files by last-accessed time:
- `not_accessed_30d` — not queried in 30 days
- `not_accessed_60d` — not queried in 60 days
- `not_accessed_90d` — not queried in 90 days

Files with no `last_accessed` value are treated as fresh (just uploaded).

### File Access Tracking

`touch_file_access(file_id)` is called on every query for each file whose sheets were loaded. This updates `files.last_accessed` to `datetime('now')`.

---

## 15. Planned: Single-Agent Refactor

### Current State

Two agents (planner + executor) with a deterministic short-circuit for pure retrievals. The planner classifies intent and produces a structured plan; the executor executes the plan using tools and generates a friendly response.

### Motivation

- **Code simplicity** — the two-agent split adds significant complexity (two system prompts, two result types, pre-population logic, short-circuit logic, hand-off prompt construction)
- **Marginal latency savings** — the current pre-population optimization already collapses retrievals into pure Python; the executor only handles compute + friendly response (1 LLM call). A single agent would do the same in 1-2 LLM calls depending on tool usage.

### Proposed Design

**Single agent** with all tools (`retrieve`, `retrieve_batch`, `execute_python_code`) and a comprehensive system prompt that handles both planning and execution:

1. **Simple retrieval queries** — agent calls `retrieve` or `retrieve_batch` directly, formats answer from the returned values. 1-2 LLM calls (tool call + response generation).
2. **Multi-step calculations** — agent calls retrieve tools, then `execute_python_code` for calculations, then formats the response. 2-3 LLM calls.
3. **Deterministic short-circuit preserved** — if the query is a simple retrieval (detected via regex/heuristics), skip the agent entirely and call `retrieve` in pure Python. Preserves the 1-call path for simple queries.

### Streaming with Single Agent

Events would be emitted from within the tool functions themselves:
- `retrieve` emits "Data Retrieved" event
- `execute_python_code` emits "Calculations Complete" event
- Agent's final response emits "Friendly Response" event

This requires tools to have access to the `on_event` callback (via `PipelineDeps` or context).

### Trade-offs

| Aspect | 2 Agents (current) | 1 Agent (proposed) |
|--------|-------------------|-------------------|
| Simple retrieval | 1 LLM call (planner only, short-circuit) | 0 LLM calls (deterministic short-circuit) or 2 (agent calls retrieve + formats) |
| Complex calculation | 2 LLM calls (planner + executor) | 2-3 LLM calls (retrieve + compute + format) |
| Code complexity | High (two prompts, two models, hand-off logic) | Lower (one prompt, one agent, tools emit events) |
| Plan visibility | Structured QueryPlan available for UI display | No explicit plan; agent decides internally |
| Maintainability | Changes require updating two agents in sync | Single agent to update |

### Key Risk

Loss of explicit plan visibility in the UI. Currently the `plan` SSE event shows the user exactly what steps will be executed. With a single agent, there's no structured plan — the agent just calls tools iteratively. Mitigation: emit tool-call events as "steps" so the user still sees progress, just without a pre-execution plan.
