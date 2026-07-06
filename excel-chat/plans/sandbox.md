# Sandbox Architecture & Deployment Plan

**Purpose:** This document covers the design, tradeoffs, and production roadmap for the Python code execution sandbox used by the executor agent to run LLM-generated calculations.

---

## 1. Purpose

The Financial Analyst AI pipeline includes an **executor agent** that retrieves values from uploaded spreadsheets and then performs calculations on them. Rather than relying on the LLM to do arithmetic in its head (error-prone for multi-step calculations), the executor generates Python code and submits it to a **sandbox** for safe execution.

The sandbox must:
- Execute arbitrary LLM-generated Python code without crashing the server
- Prevent filesystem access, network calls, and module imports
- Return structured results (return value or stdout) back to the executor
- Be fast enough to not bottleneck the query pipeline (LLM calls already take 1–5+ seconds)

---

## 2. Current Implementation

### 2.1 Technology: pydantic-monty

The sandbox uses **[`pydantic-monty`](https://github.com/pydantic/monty)** (`Monty` class) — a lightweight Python sandbox library that interprets code via AST parsing in a restricted namespace. It does **not** use `exec()` or `eval()`.

**Isolation properties:**
- No `import` statement — LLM cannot import arbitrary modules
- No file I/O — `open()`, `os`, `subprocess` are not available
- No network access — `socket`, `urllib`, `requests` are not available
- No global state mutation — sandbox runs in an isolated namespace

### 2.2 Deployment Model: In-Process

The sandbox runs **inside the FastAPI backend process**. It is a Python library (`pydantic-monty` in `requirements.txt`), not a separate service. The `execute_python_code` tool function in `pipeline.py` instantiates `Monty` directly and calls `await m.run_async()`.

### 2.3 Code Processing Pipeline

When the executor agent calls `execute_python_code(code)`:

```
LLM generates code string
  │
  ├─ 1. Cache check (SHA-256 of normalized code)
  │     HIT → return cached output immediately
  │     MISS → continue
  │
  ├─ 2. Dedent: textwrap.dedent(code).strip()
  │     Removes common leading whitespace from multi-line strings.
  │
  ├─ 3. Return-statement injection
  │     If code doesn't start with `return`, check for assignments to
  │     `result` and convert the last assignment to a `return` statement.
  │
  ├─ 4. Type stubs injection
  │     Pre-inject type definitions for computed values from the pipeline
  │     (e.g., `step1: float = 0.0`) so Monty's type checker doesn't reject
  │     references to pre-populated values.
  │
  ├─ 5. Monty instance creation
  │     m = pydantic_monty.Monty(
  │         code, inputs=[], script_name="sandbox.py",
  │         type_check=False, type_check_stubs=type_defs,
  │     )
  │
  ├─ 6. stdout capture
  │     sys.stdout redirected to io.StringIO() so print() output is
  │     captured as fallback if no return statement produces a value.
  │
  ├─ 7. Execution: await m.run_async(inputs={}, external_functions={})
  │     Runs the code asynchronously in the sandbox.
  │
  ├─ 8. Result extraction
  │     - return value not None → str(output)
  │     - stdout has content → stdout_output.strip()
  │     - else → "Code executed successfully (no output)"
  │
  ├─ 9. Cache store: sandbox_cache_set(user_id, code, result)
  │
  └─ 10. stdout restore (in finally block)
```

### 2.4 What's Available in the Sandbox

| Category | Available | NOT Available |
|----------|-----------|---------------|
| **Syntax** | Variables, arithmetic (`+`, `-`, `*`, `/`, `**`, `//`, `%`), conditionals (`if/else`), loops (`for`, `while`), list/dict comprehensions, f-strings | `import`, `class`, decorators, `yield`, `async/await` |
| **Modules** | `math` (pre-injected: `math.sqrt`, `math.pow`, `math.log`, `math.exp`, `math.ceil`, `math.floor`, etc.) | `os`, `sys`, `subprocess`, `socket`, `urllib`, `json`, `pickle`, `open()` |
| **Builtins** | `abs`, `round`, `min`, `max`, `sum`, `len`, `sorted`, `range`, `int`, `float`, `str`, `list`, `dict`, `tuple`, `set`, `bool`, `enumerate`, `zip`, `map`, `filter` | `exec`, `eval`, `compile`, `globals`, `locals`, `__import__`, `getattr` (on dangerous objects) |
| **Statistics** | `statistics` module (mean, median, stdev, variance) — available if injected | `numpy`, `pandas`, `scipy` (not injected) |
| **External functions** | Empty dict `{}` — no external functions are passed in | Any function from the host application |

### 2.5 Caching

Sandbox results are cached in **Layer 2** of the caching stack (see `scaling.md`):
- **Key:** SHA-256 hash of normalized code (comments stripped, whitespace normalized)
- **Storage:** Redis (primary) with SQLite fallback
- **TTL:** Same as result cache (default 1 hour)
- **Hit rate:** High for identical calculations across users (e.g., common ratio analyses)

---

## 3. In-Process vs. Separate Container: Tradeoff Analysis

### 3.1 In-Process (Current)

| Aspect | Assessment |
|--------|------------|
| **Latency** | ~0ms network overhead — code executes immediately in the same process |
| **Deployment** | One container, one Dockerfile, one Railway service |
| **Debugging** | Easy — stdout capture, stack traces, breakpoints all in one process |
| **Cost** | No additional service to run |
| **Cache locality** | Sandbox cache (Redis/SQLite) is co-located, no extra hop |

**Risks:**
- **No OS-level isolation** — a sandbox escape (bug in `pydantic-monty`) exposes the entire backend: env vars (API keys, S3 credentials), database connections, all user data
- **Resource contention** — a CPU-heavy LLM-generated computation (infinite loop, large matrix math) blocks the FastAPI event loop and degrades response times for *all* users
- **Memory pressure** — a memory-heavy sandbox execution can OOM-kill the entire backend container, taking down the API for everyone
- **No independent scaling** — if sandbox executions are the bottleneck, you can't scale just the sandbox; you must scale the entire backend (with all its LLM API calls, S3 access, etc.)
- **No independent updates** — patching or upgrading the sandbox requires redeploying the entire backend

### 3.2 Separate Container

| Aspect | Assessment |
|--------|------------|
| **Latency** | HTTP/gRPC round-trip adds ~5–50ms per execution. For 3–4 sandbox calls per query, that's 15–200ms overhead — noticeable but minor given LLM calls already take 1–5+ seconds |
| **Isolation** | OS-level boundary — sandbox container has no access to API keys, database, S3 credentials, or user data. Can run with restricted IAM, read-only filesystem, no network egress |
| **Scaling** | Scale sandbox replicas independently based on CPU/memory pressure. 1 backend + 3 sandbox workers if computations are the bottleneck |
| **Resource limits** | Hard CPU/memory limits per sandbox container. A runaway execution only affects that one container; the backend stays responsive |
| **Deployment** | Independent — upgrade `pydantic-monty`, swap sandbox libraries, or add `nsjail`/gVisor wrapping without touching the backend |
| **Observability** | Sandbox metrics (execution time, failure rate, resource usage) isolated and easier to monitor/alert on |

**Costs:**
- Operational complexity — another service to deploy, monitor, scale, debug
- Serialization overhead — code string + computed values must be serialized (JSON/protobuf) and deserialized
- New failure modes — network timeouts, sandbox container crashes, connection pool exhaustion
- Additional cost — container(s) running continuously, even when idle

### 3.3 Decision Matrix

| Criterion | In-Process | Separate Container |
|-----------|-----------|-------------------|
| Latency | ✅ ~0ms | ⚠️ +15–200ms per query |
| Security | ⚠️ AST-only isolation | ✅ OS-level isolation |
| Resource isolation | ❌ Shared process | ✅ Hard limits per container |
| Independent scaling | ❌ All-or-nothing | ✅ Scale sandbox separately |
| Operational complexity | ✅ Simple | ⚠️ Additional service |
| Cost | ✅ No extra | ⚠️ Additional container(s) |
| Debugging | ✅ One process | ⚠️ Cross-service tracing |
| Current stage fit | ✅ Good for capstone | ⚠️ Overkill for now |

---

## 4. Production Roadmap

### Phase 1: In-Process Hardening (Current → Short-Term)

Keep the sandbox in-process but add safety guardrails:

1. **Execution timeout** — wrap `m.run_async()` with `asyncio.wait_for(..., timeout=10)` to prevent infinite loops
2. **Memory limits** — use `resource.setrlimit(RLIMIT_AS, ...)` on Linux to cap memory per execution
3. **CPU limits** — use `resource.setrlimit(RLIMIT_CPU, ...)` to cap CPU seconds
4. **Clean interface** — extract sandbox execution into a dedicated module with a clear interface:

   ```python
   # sandbox_runner.py
   async def run_sandbox(code: str, computed_values: dict[str, Any]) -> str:
       """Execute Python code in a secure sandbox.
       
       This function is the single entry point for sandbox execution.
       The implementation can be swapped from in-process to remote
       without changing any callers.
       """
       ...
   ```

5. **Structured error responses** — return error types (timeout, memory, syntax, runtime) so the executor agent can react intelligently

### Phase 2: Subprocess Isolation (Medium-Term)

When user base grows or untrusted inputs become a concern:

1. **Run sandbox in a subprocess** — `multiprocessing` or `subprocess` with `nsjail`/`bubblewrap` wrapper
2. **Hard kill on timeout** — subprocess can be forcefully terminated without affecting the main process
3. **Resource cgroups** — Linux cgroups to enforce CPU/memory limits at the OS level
4. **No change to callers** — the `run_sandbox()` interface stays the same

### Phase 3: Separate Container (Long-Term)

When sandbox executions become a scaling bottleneck or for full production hardening:

1. **Deploy sandbox as a separate Railway service** — small container with `python:*-slim`, `pydantic-monty`, and a FastAPI/gRPC endpoint
2. **API contract:**

   ```
   POST /execute
   {
     "code": "revenue = 1500000\nexpenses = 800000\nmargin = (revenue - expenses) / revenue * 100\nreturn margin",
     "computed_values": {"step1": 1500000.0, "step2": 800000.0}
   }
   
   200 OK
   {
     "result": "46.666666666666664",
     "execution_time_ms": 12,
     "stdout": ""
   }
   
   408 Timeout
   {
     "error": "execution_timeout",
     "timeout_seconds": 10
   }
   ```

3. **Connection pooling** — backend maintains a pool of sandbox connections to amortize connection overhead
4. **Auto-scaling** — scale sandbox replicas based on queue depth or CPU utilization
5. **Health checks** — sandbox service exposes `/health` endpoint for Railway to monitor
6. **No network egress** — sandbox container has no outbound network rules except to the backend
7. **Read-only filesystem** — sandbox container runs with a read-only root filesystem

### Architecture Diagram (Phase 3)

```
┌─────────────────────────────────────────────────────────────┐
│                     Backend Container                        │
│                                                              │
│  FastAPI                                                     │
│    ├─ /upload/                                               │
│    ├─ /query                                                 │
│    └─ /query/stream                                          │
│                                                              │
│  Pipeline                                                    │
│    ├─ Planner Agent ──→ LLM API                              │
│    ├─ Executor Agent ──→ LLM API                             │
│    │    ├─ retrieve()  ──→ S3 / DataFrame                    │
│    │    └─ run_sandbox() ──────┐                             │
│    └─ Responder Agent ──→ LLM API                            │
│                                │                             │
│  sandbox_runner.py             │ HTTP POST /execute          │
│    run_sandbox(code, values) ──┼─────────────────────────────┼──→
│                                │                             │
└────────────────────────────────┼─────────────────────────────┘
                                 │
                    ┌────────────▼───────────────┐
                    │    Sandbox Container(s)     │
                    │                             │
                    │  FastAPI / gRPC server      │
                    │    POST /execute            │
                    │       │                     │
                    │       ├─ Cache check        │
                    │       ├─ Monty.run_async()  │
                    │       │   (timeout: 10s)    │
                    │       ├─ Cache store        │
                    │       └─ Return result      │
                    │                             │
                    │  Constraints:               │
                    │    - No network egress      │
                    │    - Read-only filesystem   │
                    │    - 512MB memory limit     │
                    │    - 10s CPU timeout        │
                    └─────────────────────────────┘
```

---

## 5. Design Decisions

### Why pydantic-monty over alternatives?

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **pydantic-monty** (current) | AST-based, no `exec()`, lightweight, async support | Limited module support, no numpy/pandas | ✅ Chosen — sufficient for financial calculations |
| **RestrictedPython** | Mature, widely used, customizable policy | Still uses Python bytecode execution, less isolated | Considered — similar threat model but less ergonomic API |
| **Docker-in-Docker** | Full OS isolation | Heavy, slow startup (~1s per container), complex | Overkill for current stage |
| **WebAssembly (Pyodide)** | True sandbox, no Python runtime needed | ~40MB download, no `math` module natively, slow startup | Interesting but impractical for latency-sensitive path |
| **nsjail + subprocess** | OS-level isolation, fast (~10ms startup) | Linux-only, requires kernel features | Good Phase 2 candidate |

### Why not use `exec()` with a restricted `__builtins__`?

`exec()` with a custom `globals` dict is the "DIY sandbox" approach. It is **not safe** because:
- `__builtins__` can be re-imported via `().__class__.__bases__[0].__subclasses__()`
- Attribute access chains can escape to `os.system`
- It's a well-known Python security anti-pattern

`pydantic-monty` avoids this entirely by parsing the AST and evaluating it with a controlled interpreter, never producing real Python bytecode.

### Why `type_check=False`?

The LLM generates dynamic code that may use patterns strict type checkers reject (e.g., assigning different types to the same variable). Disabling strict type checking avoids false rejections while the AST-level isolation still prevents dangerous operations.

---

## 6. Monitoring & Observability

### Current

- Sandbox execution results are logged via `print()` in the backend container
- Cache hit/miss is implicit (no explicit metric)
- No execution time tracking

### Recommended (Phase 1+)

| Metric | Type | Description |
|--------|------|-------------|
| `sandbox.execution.count` | Counter | Total sandbox executions |
| `sandbox.execution.duration_ms` | Histogram | Execution time per call |
| `sandbox.cache.hit_rate` | Gauge | Percentage of cache hits |
| `sandbox.error.rate` | Gauge | Percentage of executions returning errors |
| `sandbox.timeout.count` | Counter | Number of executions that timed out |
| `sandbox.memory.peak_mb` | Gauge | Peak memory usage per execution (Phase 2+) |

---

## 7. Known Limitations & Future Improvements

| Limitation | Impact | Priority | Fix |
|------------|--------|----------|-----|
| No execution timeout | Infinite loop in LLM code blocks the event loop | **High** | `asyncio.wait_for()` with 10s timeout |
| No memory limit | Memory-heavy code can OOM the container | **High** | `resource.setrlimit()` or subprocess with cgroups |
| No numpy/pandas in sandbox | Can't do vectorized calculations | Medium | Inject numpy as external module (if safe) |
| stdout redirect is not thread-safe | Concurrent sandbox calls may interleave stdout | Medium | Use per-execution `StringIO` (already done) or remove stdout capture |
| No structured error types | Executor can't distinguish timeout vs. syntax error | Medium | Return structured error objects |
| No per-user rate limiting | One user can flood sandbox with heavy computations | Low (Phase 2) | Token bucket per user in sandbox runner |

---

**End of Sandbox Architecture Document**
