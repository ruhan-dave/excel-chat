# Bugs Encountered & Resolved

A log of bugs encountered during development and deployment of RagSheets.

---

## Bug 1: Dockerfile Base Image CVE Vulnerabilities

**Status:** Fixed (`557bdcf`)
**Date:** 2026-06-29
**Severity:** Critical

### Symptom

Railway's container vulnerability scanner reported 1 critical and 2 high CVEs in the built image, traced to `backend/Dockerfile` line 17.

### Root Cause

`python:3.12-slim` is a **floating tag** — it resolves to whatever build Docker Hub last pushed, which may be weeks or months old. Debian packages inside that image (e.g. `openssl`, `libcurl`, `glibc`) accumulate CVEs over time. The `apt-get dist-upgrade` in the Dockerfile helps but can't fix packages where the Debian security team hasn't shipped a patched version yet.

### Fix

Pinned the base image to `python:3.12.12-slim-bookworm` — locking both the Python patch version (latest 3.12 release) and the Debian codename (bookworm). This ensures reproducible builds with the most recent security patches.

**File:** `backend/Dockerfile:17`
```dockerfile
# Before
FROM python:3.12-slim

# After
FROM python:3.12.12-slim-bookworm
```

---

## Bug 2: `[object Object]` Displayed in Calculation Steps

**Status:** Fixed (uncommitted)
**Date:** 2026-06-29
**Severity:** High

### Symptom

When querying financial data, the "Calculation Steps" panel showed `[object Object]` for `step_results` and `final_answer` fields instead of readable content.

### Root Cause

Two compounding issues:

1. **Frontend**: `frontend/ragsheets/src/components/ui/promptinput.tsx` line 47 used `String(value)` to render all values. When the backend returns `step_results` or `final_answer` as a dict, JavaScript's `String()` produces `[object Object]`.

2. **Backend**: The `ExecutionResult` model in `backend/src/pipeline.py` typed `final_answer` as `Any`, so the LLM executor agent sometimes returned a dict (e.g. `{"year": "2020", "value": 0.02}`) instead of a scalar. This dict then serialized as `[object Object]` in the frontend.

### Fix

**Frontend**: Use `JSON.stringify(value, null, 2)` for object values, `String(value)` for primitives.

**File:** `frontend/ragsheets/src/components/ui/promptinput.tsx:47-51`
```tsx
// Before
<span className="text-sm text-slate-900">{String(value)}</span>

// After
<span className="text-sm text-slate-900">
    {typeof value === 'object' && value !== null
        ? JSON.stringify(value, null, 2)
        : String(value)}
</span>
```

**Backend**: Added a `field_validator` on `ExecutionResult` that flattens dict/list values in `final_answer` and `step_results` to readable strings before they reach the frontend.

**File:** `backend/src/pipeline.py:194-230`

---

## Bug 3: Blank UI on Second Question (Semantic Cache Returns Wrong Shape)

**Status:** Fixed (uncommitted)
**Date:** 2026-06-29
**Severity:** High

### Symptom

The first query worked fine, but the second question (or a paraphrased version of the first) caused the entire UI to go blank — no "Answer" block, no "Calculation Steps" block.

### Root Cause

The semantic cache in `backend/src/main.py` had two compounding problems:

1. **Storage** (line ~421): Stored only `str(result.get("answer"))` — a stringified dict — discarding `friendly_response` entirely.
2. **Retrieval** (line ~325): Returned the cached string directly as `answer`. The frontend's `Object.keys(answer).length === 0` check on a string evaluated to `0`, so `answerBlock` was null. No `friendly_response` field meant `friendlyBlock` was also null. Both blocks null → completely blank UI.

### Fix

- **Storage**: Serialize the entire `result` dict as JSON (`json.dumps(result)`) so both `answer` and `friendly_response` are preserved.
- **Retrieval**: Parse the cached JSON back (`json.loads`) and return both `answer` and `friendly_response` as separate fields, matching the non-cached response shape.

**File:** `backend/src/main.py` — semantic cache store and retrieval logic.

**Note:** Existing cached entries (stored as plain strings before the fix) will return `friendly_response: ""` — the Answer block won't show for old cache entries, but Calculation Steps will. New queries cache correctly. Clear old entries via `POST /api/cache/cleanup` after deploying.

---

## Bug 4: Alpine Base Image Incompatible with ML Packages

**Status:** Fixed (`9396542`)
**Date:** 2026-06-28
**Severity:** Critical

### Symptom

Container build failed or container crashed at startup with `ModuleNotFoundError` for ML packages like `onnxruntime`, `chromadb`, or `torch`.

### Root Cause

The Dockerfile used `python:3.12-alpine`. Alpine Linux uses `musllinux` instead of `glibc`. Many ML/AI Python packages only ship `manylinux` wheels (compiled against glibc). pip cannot install them on Alpine, so they either fail to build from source or are missing entirely.

### Fix

Switched from `python:3.12-alpine` to `python:3.12-slim` (Debian-based). Updated system package commands accordingly: `apk` → `apt-get`, `addgroup -S` → `groupadd`, `adduser -S` → `useradd`.

**File:** `backend/Dockerfile`

---

## Bug 5: Missing `pydantic-ai` in `requirements.txt`

**Status:** Fixed (`732cb87`)
**Date:** 2026-06-28
**Severity:** Critical

### Symptom

Container crashed immediately on startup with `ModuleNotFoundError: No module named 'pydantic_ai'`.

### Root Cause

`pydantic-ai` was installed in the local development environment (via pip/conda) but was missing from `backend/requirements.txt`. The container only installs packages listed in `requirements.txt`, so the import failed at runtime.

### Fix

Added `pydantic-ai==0.0.15` to `backend/requirements.txt`.

**Lesson:** Always verify all imports in entry point files are pinned in `requirements.txt` before deploying.

---

## Bug 6: Railway `railway.toml` — Unsupported Root-Level Config

**Status:** Fixed (`e35866f`, `e794cc6`)
**Date:** 2026-06-28
**Severity:** High

### Symptom

Railway ignored the `railway.toml` file and used Railpack auto-detection instead of the custom Dockerfile. The build used wrong settings or failed.

### Root Cause

Two issues:
1. A single `railway.toml` at the repo root tried to configure multiple services using `[services.*]` syntax, which Railway doesn't support. Railway's config-as-code only supports `[build]` and `[deploy]` top-level sections — one file per service.
2. Without `source.rootDirectory` set, Railway analyzed the repo root and auto-detected the build, ignoring the Dockerfile in subdirectories.

### Fix

Created per-service `railway.toml` files (`backend/railway.toml`, `frontend/railway.toml`), each with explicit `builder = "DOCKERFILE"` and `dockerfilePath = "Dockerfile"`. Set `source.rootDirectory` for each service via Railway CLI.

---

## Bug 7: Railway Volume Mount Permission Denied

**Status:** Fixed (`0ae74a0`, `aa41294`)
**Date:** 2026-06-28
**Severity:** High

### Symptom

Container failed to write `sheets.db` to the persistent volume at `/app/data`, crashing on startup or on first query.

### Root Cause

The Dockerfile created a non-root user and ran the container as that user, but the Railway volume mount at `/app/data` was owned by root. The non-root user couldn't write to it.

### Fix

1. Added `RUN mkdir -p /app/data` with proper ownership in the Dockerfile.
2. Ran the container as root to ensure write access to the volume mount (acceptable for this project; a production setup would use a dedicated user with correct UID/GID).

**Files:** `backend/Dockerfile`

---

## Bug 8: Static Files Not Served by FastAPI (SPA Routing)

**Status:** Fixed (`22fc978`)
**Date:** 2026-06-28
**Severity:** Medium

### Symptom

The frontend loaded but navigating to any route other than the root returned 404. React SPA client-side routing didn't work.

### Root Cause

FastAPI's `StaticFiles` mount doesn't support SPA fallback — it returns 404 for any path that doesn't match a physical file. React Router relies on the server returning `index.html` for all non-API routes.

### Fix

Replaced the `StaticFiles` mount with a catch-all route that serves static files if they exist, or falls back to `index.html` for SPA routing.

**File:** `backend/src/main.py:557-570`

---

## Bug 9: Complex Query Calculations Failed with Rigid Parsing

**Status:** Fixed (`095b99d`, `cb2a6c7`)
**Date:** 2026-06-25
**Severity:** High

### Symptom

Queries involving nested computations or complex math operations (e.g. multi-step calculations with natural language ordering) returned wrong answers or errored out.

### Root Cause

The pipeline used strict exact-word matching to parse math operations from the plan. This rigid syntax couldn't handle nested computation or complex formulas — the parser would fail to match operations described in slightly different wording.

### Fix

Switched to an agentic approach using Pydantic AI SDK: the LLM generates code from natural-language descriptions of math operations, and a sandbox (`pydantic-monty`) executes it. This removed the need for strict parsing and enabled arbitrary complex calculations.

**File:** `backend/src/pipeline.py`

---

## Bug 10: Frontend Value Display Issue in TSX

**Status:** Fixed (`36d9c50`)
**Date:** 2026-06-20
**Severity:** Medium

### Symptom

Numeric values from query results displayed incorrectly in the frontend — values were showing as `NaN`, `undefined`, or with wrong formatting.

### Root Cause

A TypeScript type mismatch in the frontend component caused values to be rendered before they were properly parsed from the API response.

### Fix

Fixed the value parsing logic in the TSX component to correctly handle the JSON response structure.

**File:** `frontend/ragsheets/src/components/ui/promptinput.tsx`

---

## Bug 11: App Crashes on Backend Error Response (Blank Screen)

**Status:** Fixed (uncommitted)
**Date:** 2026-06-29
**Severity:** Critical

### Symptom

After redeploying, asking a question caused the entire app to go blank. No error message, no UI elements — just a white screen.

### Root Cause

When the planner agent fails validation (`Exceeded maximum retries (3) for result validation`), the backend catches the exception and returns `{"error": "..."}` with HTTP 200. The frontend's `submitQuery` called `setAnswer(response.data.answer)` — but `response.data.answer` is `undefined` when the response only has an `error` field. This set the React state to `undefined`, and the subsequent `Object.keys(answer)` in the render threw a `TypeError: Cannot convert undefined to object`, crashing the entire React component tree with no error boundary to catch it.

### Fix

1. **Frontend**: Check for `response.data.error` before accessing `answer`/`friendly_response`. On error, show the error message in the friendly response block instead of crashing. Also use `?? {}` and `?? ""` fallbacks to prevent `undefined` from ever reaching state.

2. **Backend**: Map the opaque `Exceeded maximum retries` error to a user-friendly message: "The AI model could not process this query. Please try rephrasing your question."

**Files:** `frontend/ragsheets/src/components/ui/promptinput.tsx`, `backend/src/main.py`

---

## Bug 12: Sanitized Upload Returns 0 Sheets (S3 Re-parse Fails)

**Status:** Fixed (`05395f6`)
**Date:** 2026-07-30
**Severity:** High

### Symptom

When a user uploads an Excel file containing sensitive data (e.g. SSNs), the sensitive-data sanitize path writes the cleaned DataFrame to a new Excel file and uploads it to S3. On re-parse (during `_finalize_upload` and later during query via `read_excel_from_s3`), `clean_dataframe` can't find a year row, resulting in 0 sheets. Integration tests fail with `assert 0 > 0` and queries return "No sheets uploaded."

### Root Cause

Two compounding issues:

1. **`main.py` confirm_upload**: After sanitizing, the cleaned DataFrame (with `category` as index and year strings as columns) was written to Excel with `index=False`. This discarded the category index, so when re-read by `pd.read_excel`, the first column became an unnamed integer index — no longer recognizable as a category column.

2. **`excelservices.py` load_all_sheets / load_all_sheets_buffer**: Only attempted `clean_dataframe`, which expects raw metadata rows above a year row. If the DataFrame was already cleaned (category index + year columns), `clean_dataframe` raised `ValueError("No year row found")` and the sheet was silently skipped.

### Fix

1. **`main.py:298`**: Changed `df.to_excel(writer, sheet_name=sheet_name, index=False)` to `index=True` so the category index is preserved in the Excel file.

2. **`excelservices.py:47-62`**: Added `_try_clean` helper that attempts `clean_dataframe` and falls back to returning the raw DataFrame if it's already cleaned (detected by checking for a `category` column or index). Applied to both `load_all_sheets` and `load_all_sheets_buffer`.

**Files:** `excel-chat/backend/src/main.py`, `excel-chat/backend/src/excelservices.py`

---

## Bug 13: Fallback Model Confuses `retrieve` and `retrieve_batch` Tools

**Status:** Fixed (`953e401`)
**Date:** 2026-08-01
**Severity:** High

### Symptom

When the primary model (`openai/gpt-oss-120b:nitro`) returned an empty response and the pipeline fell back to `deepseek/deepseek-v4-pro`, the fallback model would call `retrieve` with a list of years (`years`) instead of a single year string (`year`). This triggered pydantic-ai `ValidationError` — the `retrieve` tool expected `year: str` but received `years: list[str]`, and the `retrieve_batch` tool expected `years: list[str]` but received `year: str`. Integration tests (`test_trend_analysis`, `test_query_percentage_calculation`) failed intermittently depending on whether the fallback model was invoked.

### Root Cause

Two separate tools — `retrieve(ctx, field, year: str, sheet)` and `retrieve_batch(ctx, field, years: list[str], sheet)` — had nearly identical names, descriptions, and parameter sets. The only difference was `year` (singular string) vs `years` (plural list). The fallback model (deepseek-v4-pro) could not reliably distinguish between them and would send a list of years to `retrieve` or a single string to `retrieve_batch`, causing pydantic-ai's strict `extra_behavior: Forbid` validation to reject the call.

Additionally, pydantic-ai 0.0.15 generates an internal validator from the function signature that marks **all** parameters as required regardless of Python defaults. The `prepare` function only modifies the JSON schema sent to the LLM — it does not affect the internal validation schema. So even making `year` optional in the signature did not prevent the validation error.

### Fix

Merged `retrieve` and `retrieve_batch` into a single unified `retrieve_values(ctx, field, years: list[str], sheet)` tool:

- **Single year**: pass `["2023"]` — returns a plain string (e.g. `"1500.0"`)
- **Multiple years**: pass `["2020", "2021", "2022"]` — returns a JSON dict (e.g. `{"2020": 1500.0, "2021": 1200.0}`)

The tool internally delegates to `_retrieve_single` (one year) or `_retrieve_multi` (multiple years) helpers. There is now only one tool for the LLM to call, eliminating the confusion entirely.

Also removed the now-obsolete `extract_val` tool (which was a duplicate of `retrieve` with the same signature).

**Files:**
- `excel-chat/backend/src/tools.py` — replaced `retrieve`, `extract_val`, `retrieve_batch` with `retrieve_values` + helpers; replaced three prepare functions with `_prepare_retrieve_values_tool`
- `excel-chat/backend/src/pipeline.py` — updated imports, `PlanStep` action type (removed `retrieve_batch`), planner/executor system prompts, `_prepopulate_retrievals`, `_plan_is_pure_retrieve`, and executor prompt building
- `excel-chat/backend/src/classification_template.py` — updated all `retrieve_batch` references to `retrieve` with multi-year args
- `excel-chat/tests/test_optimization.py` — updated all tests to use `retrieve_values` and unified `retrieve` action

---

## Bug 14: Test Files Still Importing Removed `retrieve` Function After Tool Merge

**Date:** 2026-08-01 (CI failure), fixed 2026-08-05

**Symptom:**
CI Backend Tests job failed with `ImportError` and `NameError` on the `agentic` branch after the `retrieve`/`retrieve_batch` merge into `retrieve_values` (Bug 13). All 5 consecutive CI runs failed.

**Root Cause:**
When `retrieve` and `retrieve_batch` were merged into the unified `retrieve_values` tool in `tools.py` and `pipeline.py`, two test files were not updated:
- `excel-chat/tests/test_financial_questions.py:27` — imported `retrieve` from `pipeline`
- `excel-chat/tests/test_multi_sheet.py:15` — imported `retrieve` from `pipeline`

These files still called `retrieve(ctx, field, year, sheet)` with a string `year` argument, but the function no longer existed — it was replaced by `retrieve_values(ctx, field, years: list[str], sheet)`.

The first CI run after the merge failed with `ImportError: cannot import name 'retrieve' from 'pipeline'` (exit code 2 — collection error). After fixing the imports, a second CI run failed with `NameError: name 'retrieve' is not defined` in `test_retrieve_not_found` (exit code 1 — one missed call site).

**Fix:**
- Updated both test files to import `retrieve_values` instead of `retrieve`
- Updated all `retrieve(ctx, field, "2022", sheet)` calls to `retrieve_values(ctx, field, ["2022"], sheet)` (string → list)
- Three call sites in `test_multi_sheet.py` and one in `test_financial_questions.py`

**Files:**
- `excel-chat/tests/test_financial_questions.py` — updated import and `_retrieve_val` helper
- `excel-chat/tests/test_multi_sheet.py` — updated import and 3 `retrieve()` call sites

**Lesson:** When renaming/removing a public API function, grep all test files for imports and call sites — not just the test file that was directly modified during the feature work.

---

## Bug 15: Short-Circuit Retrieves Wrong Field for Hierarchical Items

**Status:** Fixed (`ab248c6`)
**Date:** 2026-08-15
**Severity:** High

### Symptom

Queries like "average annual expense on grants to foreign governments between 2015 and 2020" returned incorrect values — mostly 0s with only 11.48 in 2017. The actual values for "To foreign governments" were 97217.31 (2015), 112068.49 (2016), etc. The short-circuit was retrieving the parent category "Grants" instead of the subcategory "To foreign governments".

### Root Cause

The `retrieve_numbers` short-circuit in `pipeline.py` parsed planner items by splitting on commas and treating `parts[0]` as the field name and `parts[1:]` as years. But the planner returns hierarchical items like `"Grants, To foreign governments, 2015"` — a 3-part format where:
- `parts[0]` = category ("Grants")
- `parts[1]` = subcategory / actual field ("To foreign governments")
- `parts[2]` = year ("2015")

The code used `field = parts[0]` ("Grants") and `years = parts[1:]` (["To foreign governments", "2015"]), so `retrieve_values` looked up "Grants" (the parent total) instead of "To foreign governments" (the specific subcategory). This produced wrong values that were then cached and served to subsequent similar queries.

Additionally, `_format_simple_response` only handled 1-2 items (returned `None` for 3+), so the `friendly_response` was empty for multi-year queries, causing the frontend to show "Failed to get response from server."

### Fix

**Item parsing** (`pipeline.py`): The last part is always the year; the second-to-last is the field name. For 4+ parts, join the middle parts as the field name.

```python
# Before
field = parts[0]
years = parts[1:]

# After
year = parts[-1]
field = parts[-2]
if len(parts) > 3:
    field = ", ".join(parts[1:-1])
```

**Deduplication**: When all items share the same field (e.g. 6 items for "To foreign governments" across 2015-2020), retrieve once with all years instead of 6 separate calls.

**Friendly response**: Replaced `_format_simple_response` call with inline formatting that handles both deduplicated (single dict with multiple years) and multi-item (list of single-year dicts) results.

**Variable shadowing**: Renamed `all_fields`→`item_fields` and `all_years`→`item_years` in the short-circuit to avoid Python scoping errors — the enclosing `build_query_pipeline` scope already defines `all_fields` and `all_years`, and assigning to them inside `run_pipeline` makes Python treat them as local for the entire function, causing `UnboundLocalError` at the earlier `PipelineDeps` construction.

**Files:** `excel-chat/backend/src/pipeline.py`

---

## Bug 16: Semantic Cache Returns Wrong Answers for Different Year Ranges

**Status:** Fixed (`e484412`, `75c7835`)
**Date:** 2026-08-15
**Severity:** High

### Symptom

The semantic cache matched too aggressively — queries with different year ranges (e.g. "2015-2020" vs "2020-2023") would hit the cache at 97%+ semantic similarity and return the wrong cached response. Additionally, stale cache entries from Bug 15 (containing wrong values and empty `friendly_response`) persisted and kept being served even after the item parsing fix was deployed.

### Root Cause

The original cache lookup used only embedding cosine similarity (threshold 0.88) with no deterministic validation. Two queries about "grants to foreign governments" with different years would have very similar embeddings (0.97+ similarity) because the field names and intent are nearly identical — the only difference is the year numbers, which contribute minimally to the embedding vector.

The cache had no mechanism to verify that the cached response actually answered the same question — it trusted the embedding similarity alone.

### Fix

**Two-stage cache matching** (`semantic_cache.py`):

1. **Stage 1 — Semantic similarity (broad filter):** Embedding cosine similarity above threshold (0.88). This finds paraphrased queries about the same topic (e.g. "revenue in 2022" ≈ "2022 revenue").

2. **Stage 2 — Deterministic guarantee:** Exact match on:
   - **Years**: 4-digit years (1900-2099) extracted via regex from both query and cached query must be identical sets
   - **Numbers**: All non-year numbers (integers, floats) must match exactly
   - **Column/field names**: Stop-word-filtered tokens from both queries must match exactly

   If the query has no years/numbers (e.g. `query_text` not provided), that specific check is skipped for backwards compatibility.

**Helper functions added:**
- `_extract_years(text)` — regex `\b(19\d{2}|20\d{2})\b`
- `_extract_numbers(text)` — all standalone numbers, excluding years
- `_extract_columns(text)` — strips years, numbers, and stop words to isolate field names
- `_deterministic_match(query_years, cached_years, query_numbers, cached_numbers, query_columns, cached_columns)` — returns `False` if any deterministic check fails

**Cache storage** (`sheet_metadata.py`): Added `query_text` column to `llm_cache` table (with migration) to store the original prompt text alongside the embedding, enabling deterministic matching on lookup.

**Cache flush** (`main.py`): Added `POST /cache/clear` endpoint to invalidate all cached responses for a user. Used to flush 57 stale entries containing wrong values from Bug 15.

**Files:**
- `excel-chat/backend/src/semantic_cache.py` — two-stage matching, helper functions
- `excel-chat/backend/src/sheet_metadata.py` — `query_text` column, migration, `set_cached_response`, `list_user_embeddings`
- `excel-chat/backend/src/main.py` — pass `query_text` to `find_similar_cached`, `/cache/clear` endpoint
- `excel-chat/tests/test_semantic_cache.py` — tests for year mismatch rejection, same-year paraphrase matching

**Lesson:** Semantic similarity alone is insufficient for caching queries that differ only in numeric parameters (years, amounts). Use semantic matching as a broad filter, then apply deterministic exact matching on the structured elements (years, field names) as a guarantee.

