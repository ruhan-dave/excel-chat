# Latency Optimization Plan

## Current Architecture & Latency Budget

Each query flows through these stages:

```
User submits query
  → S3 download (download_from_s3 per file)
  → pandas load (load_all_sheets → read_excel)
  → Planner LLM call (deepseek/deepseek-v4-flash, structured output, 3 retries)
  → Executor LLM loop (N tool calls, each a separate LLM round-trip)
    → retrieve(field, year) × N years
    → execute_python_code(code)
    → final structured response
  → Semantic cache write (Qwen/Qwen3-Embedding-8B embed)
  → Response to frontend
```

### Estimated time per stage (observed from logs)

| Stage | Estimated Time | Bottleneck |
|-------|---------------|------------|
| S3 download + pandas load | 2-5s | Network I/O, disk I/O |
| Planner LLM call | 5-15s (single), 15-45s (with retries) | LLM latency, large system prompt |
| Executor LLM loop | 30-90s | N sequential LLM round-trips for tool calls |
| Semantic cache embed | 0s (currently failing) | Model load failure — no latency, but no caching either |
| **Total** | **40-110s** | |

The **executor agent loop** is the dominant bottleneck. For a query like "lowest capital expenditure 2018-2022", the executor makes 5+ sequential `retrieve` tool calls, each requiring a full LLM round-trip to decide the next action. This is inherent to how Pydantic AI's agent loop works: the LLM generates a tool call → the framework executes it → the result is fed back → the LLM generates the next call, and so on.

---

## Optimization 1: Batch Retrieve Tool

### Root Cause

The `retrieve` tool (`pipeline.py:278`) fetches **one field-year pair per call**. For "lowest capex 2018-2022", the executor agent calls `retrieve("Capital", "2018")`, `retrieve("Capital", "2019")`, ..., `retrieve("Capital", "2022")` — 5 separate LLM round-trips just for data retrieval. Each round-trip is 3-8 seconds on OpenRouter.

### Proposed Solution

Add a `retrieve_batch` tool that accepts a field name and a list of years (or a year range), returning all values in a single tool call. The executor then makes **1 LLM round-trip** instead of N.

```python
def retrieve_batch(
    ctx: RunContext[PipelineDeps],
    field: str,
    years: list[str],
    sheet: str = "",
) -> str:
    """Retrieve multiple year values for a field in one call.

    Args:
        field: The financial field to look up (e.g. "Capital expenditure").
        years: List of years to retrieve (e.g. ["2018", "2019", "2020"]).
        sheet: Optional sheet name to restrict search.

    Returns JSON like: {"2018": 1500.0, "2019": 1200.0, ...}
    """
```

### How This Optimizes

- **Before**: 5 years × 1 LLM round-trip each = 5 round-trips = 15-40s
- **After**: 1 batch call = 1 LLM round-trip = 3-8s
- **Savings**: 10-32s per multi-year query
- **Cascading benefit**: Fewer tool calls means fewer chances for the LLM to hallucinate or retry, reducing the `result_retries` failure mode

### Implementation Details

1. Add `retrieve_batch` function in `pipeline.py` next to `retrieve`
2. Register it as a `Tool` in `build_executor_agent`
3. Update the executor system prompt to instruct: "Use `retrieve_batch` when you need multiple years for the same field. Use `retrieve` only for a single value."
4. The planner's `PlanStep` model should also be updated to support batch retrieve steps — a single step with `action: "retrieve_batch"` and `args: ["FieldName", "2018", "2019", ...]`

### Files to Change

- `backend/src/pipeline.py` — new tool function, executor agent registration, system prompt update
- `backend/src/pipeline.py` — `PlanStep` action literal (add `"retrieve_batch"`)

---

## Optimization 2: Reduce Executor LLM Round-Trips

### Root Cause

The Pydantic AI agent loop is inherently sequential: the LLM emits a tool call → framework executes it → result fed back → LLM emits next call. Even with batch retrieve, a query may need: 1 batch retrieve + 1 execute_python_code + 1 final response = 3 LLM round-trips. Each is 3-8s.

### Proposed Solution

**Pre-populate computed values before the executor runs.** Instead of having the executor call tools to retrieve values, have the planner output a complete retrieval plan, execute all retrievals in Python (no LLM needed), inject the results into the executor's prompt as literal values, and let the executor only handle the computation + response generation.

```
Current flow:
  Planner → Executor(retrieve, retrieve, retrieve, compute, respond) = 5 LLM calls

Proposed flow:
  Planner → Python retrieval (no LLM) → Executor(compute + respond) = 2 LLM calls
```

### How This Optimizes

- **Before**: 3-5 LLM round-trips in the executor loop (retrieve + compute + respond)
- **After**: 1-2 LLM round-trips (compute + respond only, or just respond if computation is simple)
- **Savings**: 6-24s per query
- The retrievals become pure Python DataFrame lookups — microseconds instead of seconds

### Implementation Details

1. After the planner returns a `QueryPlan`, iterate through `plan.items` or `plan.plan` steps
2. For each `retrieve` step, call `retrieve()` directly (Python function, no LLM)
3. Collect results into a dict: `{"Capital_2018": 1500.0, "Capital_2019": 1200.0, ...}`
4. Inject these as literal values into the executor's prompt:
   ```
   You have already retrieved these values:
   Capital_2018 = 1500.0
   Capital_2019 = 1200.0
   ...
   Use these values directly in your calculation. Do not call retrieve.
   ```
5. The executor agent still has `execute_python_code` for complex calculations
6. For simple queries (single retrieve, no computation), skip the executor entirely and return directly

### Files to Change

- `backend/src/pipeline.py` — `run_pipeline` function (lines 750-803), executor system prompt

---

## Optimization 3: Switch LLM Model to `openai/gpt-oss-120b:nitro`

### Root Cause

`deepseek/deepseek-v4-flash` via OpenRouter has moderate latency (3-8s per call). The `:nitro` tier on OpenRouter routes to the fastest available inference provider with priority routing, reducing per-call latency.

### Proposed Solution

Change the default model from `deepseek/deepseek-v4-flash` to `openai/gpt-oss-120b:nitro`.

### How This Optimizes

- **Before**: 3-8s per LLM call (deepseek-v4-flash, standard routing)
- **After**: 1-3s per LLM call (gpt-oss-120b:nitro, priority routing)
- **Savings**: 2-5s per LLM call × 2-5 calls per query = 4-25s total
- **Quality**: gpt-oss-120b is a 120B parameter model — should handle structured output (QueryPlan, ExecutionResult) at least as well as deepseek-v4-flash
- **Reliability**: The `Exceeded maximum retries (3)` validation failures (Bug 11) may reduce with a model that follows structured output schemas more reliably

### Implementation Details

1. Update `build_openrouter_model()` in `pipeline.py:267`:
   ```python
   model_name=os.environ.get("MODEL_ID", "openai/gpt-oss-120b:nitro"),
   ```
2. Update hardcoded model references in `excelservices.py:164` and `queryservices.py:51`
3. No other changes needed — OpenRouter handles the model swap transparently

### Files to Change

- `backend/src/pipeline.py:268` — default model name
- `backend/src/excelservices.py:164` — hardcoded model for auto-descriptions
- `backend/src/queryservices.py:51` — hardcoded model for RAG queries

### Risk

- `gpt-oss-120b:nitro` may have higher per-token cost than deepseek-v4-flash
- Should verify the model is available on the user's OpenRouter plan
- Can be overridden via `MODEL_ID` env var without code changes

---

## Optimization 4: Fix Sentence-Transformers Model Load Failure

### Root Cause

The semantic cache uses `Qwen/Qwen3-Embedding-8B` (`semantic_cache.py:59`), a 16GB model. The logs show:

```
⚠️ Failed to load sentence-transformers model 'Qwen/Qwen3-Embedding-8B':
The checkpoint you are trying to load has model type `qwen3` but Transformers
does not recognize this architecture.
```

The `transformers` library version in the container doesn't support the `qwen3` architecture. This causes:

1. **Failed semantic cache** — every query is a cache miss, even for rephrased questions
2. **Startup overhead** — the model download attempt (or partial download) wastes time on every container start
3. **No semantic matching** — users asking the same question with different wording get no cache benefit

### Proposed Solution

Switch to `all-MiniLM-L6-v2` (~80MB), a widely-supported model that works with all modern `transformers` versions.

### How This Optimizes

- **Before**: Model fails to load → semantic cache disabled → every query hits the full pipeline (40-110s)
- **After**: Model loads in <2s → semantic cache works → cached queries return in <1s
- **Savings for cache hits**: 40-110s → <1s (effectively instant)
- **Savings on startup**: No more failed download attempt of a 16GB model
- **Tradeoff**: Slightly lower semantic accuracy (needs 0.88 threshold instead of 0.92), but for financial queries with consistent terminology, this is negligible

### Implementation Details

1. Update `semantic_cache.py:59`:
   ```python
   _MODEL_NAME = "all-MiniLM-L6-v2"
   _EMBED_DIM = 384  # MiniLM native dim is 384
   ```
2. Update the semantic similarity threshold if needed (currently in the cache lookup logic)
3. Pre-download the model in the Dockerfile to avoid first-query latency:
   ```dockerfile
   RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
   ```
4. The model is only 80MB — adds <1s to build time and <1s to container startup

### Files to Change

- `backend/src/semantic_cache.py:59-60` — model name and embed dimension
- `backend/Dockerfile` — pre-download model in build stage
- `backend/requirements.txt` — ensure `sentence-transformers` version supports `all-MiniLM-L6-v2` (all versions do)

---

## Optimization 5: Read Excel from S3 In-Memory (No Local Download)

### Root Cause

Every query downloads Excel files from S3 to `/tmp/uploads/temp_{file_id}_{filename}` (`main.py:370-372`), then loads them with `pandas.read_excel(filepath)` (`excelservices.py:48`). This involves:

1. S3 `download_file` → disk write (network + disk I/O)
2. `read_excel(filepath)` → disk read + parse (disk I/O + CPU)
3. `os.remove(local_path)` → disk cleanup

For a 5MB Excel file, this is 2-5s of pure I/O overhead per file.

### Proposed Solution

Use `boto3`'s `get_object` to stream the file into a `BytesIO` buffer, then pass the buffer directly to `pandas.read_excel()`. No temp file, no disk I/O.

```python
import io
import boto3

def read_excel_from_s3(s3_key: str) -> dict[str, pd.DataFrame]:
    """Read an Excel file from S3 directly into pandas DataFrames without writing to disk."""
    s3 = _get_s3_client()
    response = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    buffer = io.BytesIO(response["Body"].read())
    return ExcelService.load_all_sheets_buffer(buffer)

# In ExcelService:
@staticmethod
def load_all_sheets_buffer(buffer: io.BytesIO) -> dict[str, pd.DataFrame]:
    """Load all sheets from an in-memory Excel file."""
    all_sheets = pd.read_excel(buffer, sheet_name=None, header=None)
    cleaned = {}
    for name, df in all_sheets.items():
        cleaned[name] = ExcelService._clean_sheet(df)
    return cleaned
```

### How This Optimizes

- **Before**: S3 download (2-3s) + disk write + disk read + parse + cleanup = 3-5s per file
- **After**: S3 stream (1-2s) + in-memory parse = 1-2s per file
- **Savings**: 2-3s per file per query
- **Additional benefits**:
  - No temp file cleanup needed (removes the `os.remove` loop)
  - No `/tmp` disk space pressure (important on Railway containers with limited ephemeral storage)
  - Simpler error handling (no partial downloads to clean up)

### Implementation Details

1. Add `read_excel_from_s3(s3_key)` in `sheet_metadata.py` alongside `download_from_s3`
2. Add `load_all_sheets_buffer(buffer)` in `excelservices.py` alongside `load_all_sheets`
3. Update `main.py:362-398` to use the new in-memory path:
   ```python
   file_cache: dict[str, dict[str, pd.DataFrame]] = {}  # s3_key -> sheets dict
   for meta in all_sheet_metas:
       if meta.s3_key not in file_cache:
           file_cache[meta.s3_key] = read_excel_from_s3(meta.s3_key)
       all_sheets = file_cache[meta.s3_key]
       if meta.sheet_name in all_sheets:
           sheets[meta.sheet_name] = all_sheets[meta.sheet_name]
   ```
4. Remove temp file creation and cleanup code
5. Keep `download_from_s3` for the upload flow (which needs a local file for metadata extraction)

### Files to Change

- `backend/src/sheet_metadata.py` — new `read_excel_from_s3` function
- `backend/src/excelservices.py` — new `load_all_sheets_buffer` method
- `backend/src/main.py:362-398` — replace download+load with in-memory read

---

## Optimization 6: Add Timing Logs

### Root Cause

Currently there are no timing measurements. The `print()` statements in the pipeline show what happened but not how long each stage took. Without timing data, we can't verify which optimization had the most impact or identify new bottlenecks.

### Proposed Solution

Add `time.perf_counter()` instrumentation at every stage boundary in the query pipeline.

### How This Optimizes

- **Direct**: No latency reduction — but enables data-driven optimization decisions
- **Indirect**: Makes it possible to measure the impact of Optimizations 1-5 and identify remaining bottlenecks
- **User experience**: Timing data can be surfaced in the API response so the frontend can show "Processed in 12.3s"

### Implementation Details

Add a timing context manager and instrument `run_pipeline` and `query_rag`:

```python
import time
from contextlib import contextmanager

@contextmanager
def timed(stage: str, timings: dict[str, float]):
    start = time.perf_counter()
    yield
    elapsed = time.perf_counter() - start
    timings[stage] = elapsed
    print(f"⏱️  {stage}: {elapsed:.2f}s")

# In run_pipeline:
timings = {}
with timed("planner", timings):
    plan_result = await planner.run(query)
    plan = plan_result.data

with timed("executor", timings):
    exec_result = await executor.run(exec_prompt, deps=deps)
    execution = exec_result.data

# In query_rag:
timings = {}
with timed("s3_load", timings):
    # S3 download + pandas load
    ...

with timed("pipeline", timings):
    result = await pipeline(query)
    timings.update(result.get("timings", {}))

with timed("cache_write", timings):
    # semantic cache store
    ...

print(f"⏱️  Total: {sum(timings.values()):.2f}s | Breakdown: {timings}")
return {**result, "timings": timings}
```

### Files to Change

- `backend/src/pipeline.py` — `run_pipeline` function, add timing context manager
- `backend/src/main.py` — `query_rag` function, wrap S3 load and cache write

---

## Implementation Priority

| Priority | Optimization | Estimated Savings | Effort | Risk |
|----------|-------------|-------------------|--------|------|
| **P0** | Opt 3: Switch to gpt-oss-120b:nitro | 4-25s | Low (1 line) | Low — env var override |
| **P0** | Opt 4: Fix embedding model | 40-110s (cache hits) | Low (2 lines + Dockerfile) | Low — well-tested model |
| **P1** | Opt 1: Batch retrieve tool | 10-32s | Medium (new tool + prompt) | Low — additive |
| **P1** | Opt 2: Pre-populate retrievals | 6-24s | Medium-high (restructure pipeline) | Medium — changes executor flow |
| **P2** | Opt 5: In-memory S3 read | 2-3s per file | Medium (new functions) | Low — additive |
| **P2** | Opt 6: Timing logs | 0s (enables measurement) | Low (instrumentation) | None |

### Expected Combined Impact

| Scenario | Before | After (all opts) | Savings |
|----------|--------|-----------------|---------|
| Cache hit (rephrased question) | 40-110s (no cache) | <1s | 99% |
| Multi-year query (5 years) | 50-90s | 8-15s | 70-85% |
| Simple single-value query | 20-40s | 4-8s | 75-80% |
| Complex multi-step calculation | 60-110s | 15-25s | 60-75% |

### Suggested Implementation Order

1. **Opt 3** (model swap) — 1 line change, immediate 2-5s per-call savings
2. **Opt 4** (embedding model) — 2 line change, enables semantic cache for instant cache hits
3. **Opt 6** (timing logs) — instrument before bigger changes to measure baseline
4. **Opt 1** (batch retrieve) — new tool, biggest single-query improvement
5. **Opt 2** (pre-populate retrievals) — restructure executor flow
6. **Opt 5** (in-memory S3) — incremental I/O improvement

---

## Future Optimizations (Not in Scope)

- **Connection pooling**: Reuse OpenRouter HTTP connections across queries (currently each agent creates a new `OpenAIModel`)
- **Streaming responses**: Stream the executor's `friendly_response` to the frontend as it's generated (perceived latency reduction)
- **Parallel sheet loading**: Load multiple S3 files concurrently with `asyncio.gather` (currently sequential loop in `main.py:368`)
- **Planner-executor merge**: For simple queries, skip the planner entirely and let the executor handle planning + execution in one LLM call
- **Redis vector search**: If semantic caching is restored, use RediSearch for O(log n) vector similarity instead of O(n) SQLite scan
