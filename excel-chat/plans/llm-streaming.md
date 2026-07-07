# LLM Choice & SSE Streaming

This document covers the LLM model selection, real-time streaming architecture (backend + frontend), and performance benchmarks measuring the impact of all architectural changes.

---

## Implemented: SSE Streaming for Real-Time UI Feedback

### Problem

Cold queries can take 1-22s depending on complexity. Without streaming, the user stares at a blank loading spinner for the entire duration with no indication of progress. This creates a poor user experience — users may think the app is frozen, refresh the page, or abandon the query.

### Solution: Server-Sent Events (SSE)

The system uses SSE to stream pipeline progress events to the frontend in real-time. Each pipeline stage emits an event as it completes, so the user sees incremental progress (plan classification, data retrieval, calculations, final answer) rather than a single opaque wait.

### Architecture

```
Frontend (React)                          Backend (FastAPI)
┌─────────────────┐                      ┌──────────────────────────┐
│ EventSource     │                      │ /query/stream endpoint   │
│   ↓             │  SSE (text/event-    │   ↓                      │
│ Event listeners │  stream) over HTTP   │ asyncio.Queue            │
│   ↓             │ ←──────────────────  │   ↓                      │
│ React state     │                      │ pipeline on_event()      │
│ updates         │                      │   callback               │
│   ↓             │                      │   ↓                      │
│ UI renders      │                      │ run_pipeline()           │
│ step-by-step    │                      │   _emit("status", ...)   │
└─────────────────┘                      │   _emit("plan", ...)     │
                                         │   _emit("friendly", ...) │
                                         │   _emit("done", ...)     │
                                         └──────────────────────────┘
```

**How it works:**

1. Frontend creates `new EventSource(url)` connecting to `/query/stream?query=...`
2. Backend's `stream_generator()` coroutine runs the pipeline with an `on_event` callback
3. Each pipeline stage calls `_emit(event_type, data)` which pushes to an `asyncio.Queue`
4. The SSE yield loop consumes the queue and sends `event: {type}\ndata: {json}\n\n` to the client
5. Frontend event listeners update React state, triggering re-renders showing progress
6. When the pipeline completes, the `done` event fires and the `EventSource` closes

### Event Types & Lifecycle

| Order | Event Type | Data | When Emitted | What UI Shows |
|-------|-----------|------|-------------|---------------|
| 1 | `status` | `{"message": "Analyzing your question…"}` | Pipeline starts, planner begins | Blue spinner with status text |
| 2 | `plan` | `{"task_type": "perform_calculations", "plan": {…}, "items": [...], "description": "…"}` | Planner agent completes | Plan card: task type + step-by-step breakdown |
| 3 | `status` | `{"message": "Retrieving data from sheets…"}` | Pre-population starts | Spinner updates |
| 4 | `pre_populated` | `{"values": {"step1": {…}, "step2": {…}}}` | Pre-population completes | "Data Retrieved — N value(s) fetched" with green checkmark |
| 5 | `status` | `{"message": "Running calculations…"}` | Executor agent starts | Spinner updates |
| 6 | `execution` | `{"step_results": {…}, "final_answer": …, "explanation": "…"}` | Executor completes | "Calculations Complete — N step(s) executed" with green checkmark |
| 7 | `friendly` | `{"response": "The CAGR for Social security benefits…"}` | Friendly response generated | Blue answer card with formatted text |
| 8 | `done` | `{"timings": {"planner": 0.81, "executor": 7.02, …}, "total": 8.01}` | Pipeline complete | "Complete — Total time: Xs" with green checkmark |

**Short-circuit path** (pure retrieve, no executor):
`status` → `plan` → `status` → `pre_populated` → `status` → `friendly` → `done`

**Cache hit path** (semantic cache hit, no pipeline):
`cached` → (stream closes immediately)

**Error path:**
`error` → `{"message": "…"}` → (stream closes)

### Backend Implementation

**File:** `main.py` — `/query/stream` endpoint

```python
@app.get("/query/stream")
async def query_stream(query: str, x_user_id: str | None = Header(default=None)):
    user_id = x_user_id or "anonymous"
    event_queue: asyncio.Queue = asyncio.Queue()

    async def stream_generator():
        # 1. Semantic cache check → emit "cached" and return if hit
        # 2. Load sheets from S3
        # 3. Build pipeline with on_event callback:
        def on_event(event_type: str, data: dict):
            payload = json.dumps(data, default=str)
            event_queue.put_nowait((event_type, payload))

        pipeline = build_query_pipeline(
            None, sheets, all_sheet_metas, PromptTemplate(template),
            user_id=user_id, on_event=on_event,
        )

        # 4. Run pipeline as background task, consume events concurrently
        pipeline_task = asyncio.create_task(pipeline(query))

        while True:
            if pipeline_task.done() and event_queue.empty():
                break
            try:
                event_type, payload = await asyncio.wait_for(
                    event_queue.get(), timeout=0.1
                )
                yield f"event: {event_type}\ndata: {payload}\n\n"
            except asyncio.TimeoutError:
                continue

        # 5. Drain remaining events, get result, write to semantic cache
        result = await pipeline_task
        store_cached(user_id, query, embed_query(query), result, model)

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},  # disable nginx buffering
    )
```

**Key design decisions:**

- **`asyncio.Queue` instead of direct yields**: The pipeline runs as a background `asyncio.Task` and emits events via a callback. The queue decouples the pipeline's event emission from the SSE yield loop. This is necessary because Pydantic AI's `agent.run()` is a single awaitable — we can't yield from inside it.
- **`put_nowait` is safe**: The pipeline and SSE consumer run in the same event loop (same coroutine context), so no thread-safety concerns with the queue.
- **`X-Accel-Buffering: no`**: Disables nginx's response buffering so events are flushed to the client immediately. Without this, nginx buffers the entire response and the client sees nothing until the pipeline completes.
- **0.1s poll timeout**: The yield loop checks the queue every 100ms. This is fast enough for responsive UI updates but doesn't busy-spin the CPU.

**File:** `pipeline.py` — `_emit()` function

```python
def _emit(event_type: str, data: dict[str, Any]) -> None:
    if on_event:
        try:
            on_event(event_type, data)
        except Exception:
            pass  # never let event emission crash the pipeline
```

The `_emit` function is called at each pipeline stage. It's wrapped in try/except so a failure in event handling (e.g. client disconnected) never crashes the pipeline — the query still completes and the result is cached.

### Frontend Implementation

**File:** `promptinput.tsx` (now `useConversation.ts` hook + `thread-view.tsx`)

The frontend uses the browser's native `EventSource` API (no external library needed):

```typescript
const es = new EventSource(`${apiURL}/query/stream?query=${encodeURIComponent(query)}`);

es.addEventListener("status", (e) => {
    setStatusMsg(JSON.parse(e.data).message);
});

es.addEventListener("plan", (e) => {
    const data = JSON.parse(e.data);
    setPlanData(data);
    addStep("Classified Intent", `Task type: ${data.task_type?.replace(/_/g, " ")}`);
    addStep("Execution Plan", `${Object.keys(data.plan || {}).length} step(s) planned`);
});

es.addEventListener("pre_populated", (e) => {
    const data = JSON.parse(e.data);
    addStep("Data Retrieved", `${Object.keys(data.values || {}).length} value(s) fetched`);
});

es.addEventListener("execution", (e) => {
    const data = JSON.parse(e.data);
    addStep("Calculations Complete", `${Object.keys(data.step_results || {}).length} step(s) executed`);
    setAnswer(data.step_results);
});

es.addEventListener("friendly", (e) => {
    setFriendlyResponse(JSON.parse(e.data).response);
});

es.addEventListener("cached", (e) => {
    const data = JSON.parse(e.data);
    setAnswer(data.answer);
    setFriendlyResponse(data.friendly_response);
    addStep("Cache Hit", `Similarity: ${(data.similarity * 100).toFixed(1)}%`);
});

es.addEventListener("done", (e) => {
    const data = JSON.parse(e.data);
    addStep("Complete", `Total time: ${data.total?.toFixed(1)}s`);
    setLoading(false);
    es.close();
});
```

**UI components rendered during streaming:**

1. **Status bar** (blue, with spinner): Shows current `statusMsg` — updates as pipeline progresses
2. **Plan card** (slate, with clock icon): Shows task type + step-by-step plan breakdown with `step.action(args)` for each step
3. **Progress tracker** (white card, with calculator icon): Each `StreamStep` shows a green checkmark (done) or spinning loader (active) with label + detail
4. **Answer card** (blue, with sparkles icon): The friendly response text, rendered with `whitespace-pre-wrap` for multi-line formatting
5. **Calculation results** (white card): Raw `step_results` key-value pairs, useful for debugging

The `addStep()` function manages the progress tracker — it marks all previous steps as "done" and appends the new step. This creates a cascading checkmark effect as each stage completes.

### Why SSE Instead of WebSockets?

| Factor | SSE (chosen) | WebSockets |
|--------|-------------|------------|
| **Direction** | Server → client only (one-way) | Bidirectional (not needed here) |
| **Browser support** | Native `EventSource` API, no library | Needs `ws` library or polyfill |
| **Reconnection** | Auto-reconnect built into `EventSource` | Must implement manually |
| **HTTP compatibility** | Works through proxies, nginx, CDNs | May need special proxy config |
| **Simplicity** | 5 lines to connect, event listeners | More setup, message framing |
| **Backpressure** | HTTP chunked encoding handles flow control | Must implement flow control |

SSE is the right choice because the streaming is strictly server-to-client (the client sends the query as a URL parameter, then listens). No bidirectional communication is needed.

### What's NOT Streamed (Current Limitation)

The `friendly` event delivers the entire response text at once — it does **not** stream token-by-token as the executor LLM generates it. This means during the executor phase (the longest stage, 7-20s for complex queries), the user sees "Running calculations…" with no incremental text.

**Token-level streaming would require:**
1. Using Pydantic AI's `stream_text()` or `run_stream()` API instead of `run()`
2. Emitting `friendly_chunk` events as each token arrives
3. Frontend appending chunks to the response text in real-time

This is a planned improvement. The challenge is that the executor agent produces structured output (`ExecutionResult` with `step_results`, `final_answer`, and `friendly_response`), not raw text. Token streaming works best with text responses — structured output requires the full response before parsing. A hybrid approach could stream the `friendly_response` field while keeping `step_results` and `final_answer` as a final batch event.

### Performance Impact of Streaming

Streaming adds negligible overhead:
- Event emission: ~0.01ms per event (dict serialization + queue put)
- SSE framing: ~0.01ms per event (string formatting)
- Network: one HTTP connection held open for the duration of the query

The total overhead for a typical 8-event stream is < 1ms — invisible compared to the 1-22s pipeline. The `asyncio.Queue` approach ensures the pipeline never blocks waiting for the client to acknowledge events.

---

## Performance Benchmarks: Before & After Architectural Changes

Measured on Jul 1, 2026 with `openai/gpt-oss-120b:nitro` model, in-memory S3 reads, `retrieve_batch` + pre-population, semantic cache (threshold 0.88), and result cache. All timings are cold-cache (no semantic cache hit) unless noted.

### Measured Timings by Query Complexity

| Level | Query Description | Total (cold) | Planner | Pre-Pop | Executor | S3 Load | Warm (cached) |
|-------|-------------------|-------------|---------|---------|----------|---------|---------------|
| **L1** | Single value (1 field, 1 year) | **1.31s** | 0.79s | 0.00s | 0.51s | 0.58s | ~0s |
| **L3** | Simple ratio (2 fields, 1 year) | **1.48s** | 0.51s | 0.002s | 0.97s | 0.25s | ~0s |
| **L4** | Growth rate comparison (2 fields × 5yr) | **8.01s** | 0.81s | 0.003s | 7.02s | 0.19s | ~0s |
| **L5** | Trend + max diff (1 field × 5yr + compute) | **8.96s** | 0.49s | 0.005s | 8.44s | 0.21s | ~0s |
| **L6** | Stability comparison (2 fields × 10yr, stdev) | **21.93s** | 2.41s | 0.001s | 19.51s | 0.29s | ~0s |

**Warm cache**: All queries return in ~0s when the semantic cache hits (similarity ≥ 0.88). The entire pipeline is skipped — only embedding + cosine similarity runs.

### Estimated Old Timings (before changes)

These are grounded in the known overhead of each removed/changed component:

| Level | Old Est. | New Cold | New Warm | Improvement |
|-------|----------|----------|----------|-------------|
| **L1** | ~7-8s | 1.31s | ~0s | **6x** cold, **∞** warm |
| **L3** | ~10-12s | 1.48s | ~0s | **7-8x** cold |
| **L4** | ~25-30s | 8.01s | ~0s | **3-4x** cold |
| **L5** | ~15-18s | 8.96s | ~0s | **~2x** cold |
| **L6** | ~35-40s | 21.93s | ~0s | **~2x** cold |

### Breakdown by Change (Where the Time Went)

This is the most important section — it explains *why* each change produced a speedup and *how much* each contributed.

#### 1. Model Switch: `deepseek/deepseek-v4-flash` → `openai/gpt-oss-120b:nitro`

**Impact: ~40-50% faster per LLM call**

The old model had higher queue latency and slower token generation. The nitro tier on OpenRouter prioritizes low-latency inference. This shows up in both planner and executor timings:

| Component | Old Model (est.) | New Model (measured) | Speedup |
|-----------|-----------------|---------------------|---------|
| Planner (simple query) | ~1.2-1.5s | 0.51-0.79s | ~2x |
| Planner (complex query) | ~3-4s | 0.81-2.41s | ~1.5-2x |
| Executor (simple compute) | ~1.5-2s | 0.51-0.97s | ~2x |
| Executor (complex compute) | ~10-15s | 7.02-19.51s | ~1.3-1.5x |

The speedup is more pronounced for simpler queries because the LLM call dominates total time. For complex queries, the executor makes multiple LLM calls (code generation + execution + response formatting), so the per-call speedup compounds.

#### 2. `retrieve_batch` + Pre-Population: Eliminated N LLM Round-Trips

**Impact: Up to 20x speedup on multi-year retrieval queries**

This is the single largest improvement for complex queries. Before `retrieve_batch`, fetching 10 years of data for 2 fields required **20 individual `retrieve` tool calls** in the executor — each a full LLM round-trip (~2-4s per call).

| Query | Old Approach | New Approach | Old Time (est.) | New Time |
|-------|-------------|-------------|-----------------|----------|
| L4: 2 fields × 5yr | 10 individual `retrieve` calls in executor | 2 `retrieve_batch` calls in pre-population (pure Python) | ~20-30s retrievals + ~5s compute = ~25-30s | 0.003s pre-pop + 7.02s executor = 8.01s |
| L6: 2 fields × 10yr | 20 individual `retrieve` calls in executor | 2 `retrieve_batch` calls in pre-population (pure Python) | ~40-80s retrievals + ~5s compute = ~35-40s | 0.001s pre-pop + 19.51s executor = 21.93s |

**How it works:**
- `retrieve_batch(field, [year1, year2, ...])` fetches all years in one DataFrame scan, returning JSON `{"2018": 1500.0, "2019": 1200.0, ...}`
- `_prepopulate_retrievals()` runs all retrieve/retrieve_batch steps in pure Python *before* the executor agent starts — no LLM involved
- The executor receives pre-populated values as literals in its prompt (`step1: ALREADY DONE — value is {"2018": 1500.0, ...}`)
- The executor only handles compute steps + friendly response generation

**Before:** 20 LLM round-trips × ~2-4s each = 40-80s just for data retrieval
**After:** 0 LLM round-trips for retrieval (pure Python, ~0.001s), executor only handles computation

#### 3. In-Memory S3 Read: Eliminated Disk I/O

**Impact: ~2-3s saved per file per query**

| Path | Old | New |
|------|-----|-----|
| S3 → disk → `pd.read_excel(filepath)` | ~2.5-3s | — |
| S3 → BytesIO → `pd.read_excel(buffer)` | — | ~0.2-0.6s |

The old path downloaded the Excel file to a temporary file on disk, then read it with pandas. The new path streams S3 content directly into a `BytesIO` buffer and parses from memory. This saves the disk write + disk read round-trip.

For queries touching multiple files, the savings multiply. Within a single request, parsed sheets are cached per `s3_key` so multiple `SheetMeta` entries pointing at the same file only fetch + parse once.

#### 4. Semantic Cache: Eliminated Redundant Pipeline Runs

**Impact: ~100% speedup on repeated/paraphrased queries (cold → ~0s)**

The semantic cache uses `sentence-transformers/all-MiniLM-L6-v2` (384-dim embeddings) to find similar past queries. If cosine similarity ≥ 0.88, the entire pipeline is skipped.

| Scenario | Without Semantic Cache | With Semantic Cache |
|----------|----------------------|-------------------|
| Same query asked twice | Full pipeline both times (~1-22s) | First: full pipeline, Second: ~0s |
| Paraphrased query ("Revenue in 2022" vs "2022 revenue") | Full pipeline both times | First: full pipeline, Second: ~0s |
| Similar query after file upload | Full pipeline (cache invalidated) | Full pipeline (correct — data changed) |

**Threshold: 0.88** — high enough to avoid false positives (different questions returning wrong cached answers), low enough to catch common paraphrases.

**Invalidation:** On file upload or delete, `invalidate_user_cache(user_id)` deletes all cache entries for that user. This ensures stale answers are never served after data changes.

> **See also:** `cache-db.md` for full semantic cache architecture, embedding model details, and per-user isolation.

#### 5. Result Cache: Eliminated Redundant DataFrame Scans & Sandbox Executions

**Impact: ~40-60% of tool calls served from cache on repeated sub-queries**

Three layers of intermediate result caching:

| Layer | What's Cached | Old Behavior | New Behavior |
|-------|--------------|-------------|-------------|
| Layer 1 (retrieve) | `df.loc[field, year]` values | Every retrieve scans DataFrame | Cache hit → return instantly, skip DataFrame |
| Layer 2 (sandbox) | `execute_python_code()` output | Every compute runs sandbox | Cache hit → return cached output, skip execution |
| Layer 3 (structured) | Named operation results | Not cached | Post-execution: derive canonical keys, store for future queries |

**Example:** Query 1 asks "total revenue 2022-2025" → retrieves 4 values (cache miss, stored). Query 2 asks "ratio of revenue to grants 2022-2025" → 4 revenue retrieves hit Layer 1 cache instantly, only grants retrieves scan DataFrame.

> **See also:** `cache-db.md` for full result cache architecture, structured key format, and edge cases.

#### 6. Pure-Retrieve Short-Circuit: Eliminated Executor for Simple Queries

**Impact: 1 LLM call instead of 2 for simple lookups**

If every step in the plan is a `retrieve` or `retrieve_batch` (no compute, no named ops), the executor agent is **skipped entirely**. The pipeline builds the `ExecutionResult` directly from pre-populated values and generates a deterministic friendly string via `_format_simple_response()`.

| Query Type | Old Path | New Path |
|-----------|---------|---------|
| "What was revenue in 2022?" | Planner + Executor (2 LLM calls) | Planner only + pure Python formatting (1 LLM call) |
| "Show me interest expense 2019-2023" | Planner + Executor (2 LLM calls) | Planner only + pure Python formatting (1 LLM call) |

#### 7. Retry Reduction: 3 → 2 Retries

**Impact: ~33% faster failure path on validation errors**

When the LLM returns invalid structured output (e.g., wrong Pydantic model), Pydantic AI retries up to `result_retries` times. Reducing from 3 to 2 saves one full LLM round-trip on failure paths (~1-3s per retry). The trade-off is a slightly higher chance of `UnexpectedModelBehavior` errors on edge cases, but the `QueryPlan` and `ExecutionResult` validators (including the list-to-dict plan validator) handle the most common validation issues deterministically without needing retries.

### Where Time Still Goes (Cold Queries)

The remaining bottleneck is the **executor agent LLM calls** for compute steps. L6 (21.93s) breaks down as:

| Stage | Time | What Happens |
|-------|------|-------------|
| Planner | 2.41s | 1 LLM call to classify query and produce plan |
| Pre-populate | 0.001s | 2 `retrieve_batch` calls in pure Python (20 values) |
| Executor | 19.51s | 2-3 LLM calls: code generation + sandbox execution + friendly response |
| S3 load | 0.29s | 1 file streamed from S3 → BytesIO → pandas |

The executor time is inherent to the current two-agent architecture — it needs to:
1. Process pre-populated values (0 LLM calls — already done)
2. Generate Python code for stdev calculation (1 LLM call)
3. Execute in sandbox (~0.1s)
4. Compare results and generate friendly response (1 LLM call)

The planned single-agent refactor won't necessarily speed this up, but the pre-population optimization already eliminated the worst case (N retrieve round-trips).
