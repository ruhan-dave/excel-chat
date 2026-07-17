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

## Bug 12: Planner Not Using `retrieve_batch` for Multi-Year Retrievals

**Status:** Fixed (uncommitted)
**Date:** 2026-06-30
**Severity:** High

### Symptom

Query 7 ("Which showed greater stability over 2012-2021: wages and salaries or employers' social contributions?") took 8.25s in the executor because the planner created 20 individual `retrieve` steps (10 years × 2 fields) instead of 2 `retrieve_batch` calls. Each `retrieve` step became a separate tool call round-trip in the executor.

### Root Cause

The planner prompt in `classification_template.py` mentioned `retrieve_batch` as an option but didn't strongly enforce it. The examples section only showed `retrieve_batch` in one example, and the special handling section didn't explicitly call out multi-year scenarios. The LLM defaulted to individual `retrieve` steps for most multi-year queries.

### Fix

1. Added a **CRITICAL — retrieve_batch usage** section to the planner prompt with explicit rules:
   - "When a calculation needs 2+ years for the SAME field, ALWAYS use retrieve_batch"
   - Specific guidance for average/mean, stability/variability, trend analysis, and growth rate comparison queries
   - Wrong vs. right examples
2. Added 3 new worked examples to the prompt:
   - Example 3b: Average over multiple years using `retrieve_batch` + `compute`
   - Example 3c: Stability comparison using `retrieve_batch` + `compute` (stdev)
   - Example 3d: Growth rate comparison using `retrieve_batch` + `compute` (CAGR)
3. Expanded the "Special Handling" keyword list to include: `stability`, `variability`, `trend`, `compare`, `comparison`, `larger`, `smaller`, `increase`, `decrease`

**File:** `backend/src/classification_template.py`

### Verification

- Query 7: Executor time reduced (planner now uses 2 `retrieve_batch` calls instead of 20 `retrieve` steps)
- Query 6: Now uses 2 `retrieve_batch` calls (Social security benefits + Social assistance benefits)
- Query 8: Now uses 1 `retrieve_batch` call for 5 years of Interest expense
- Query 9: Now classified as `perform_calculations` with `retrieve_batch` + `compute` instead of `retrieve_numbers`

---

## Bug 13: Executor Friendly Response Uses Generic "First/Second Series" Instead of Field Names

**Status:** Fixed (uncommitted)
**Date:** 2026-06-30
**Severity:** Medium

### Symptom

Query 6 ("Compare the growth rates of social security benefits versus social assistance benefits from 2017 to 2021") returned: *"The compound annual growth rate (CAGR) for the first benefit series is about 25.4% per year, while the second series grew at about 21.0% per year."* — using "first/second series" instead of the actual field names.

### Root Cause

The executor agent received pre-populated values as bare numbers (e.g. `step1: 344.98`) without any context about which field name each step corresponded to. The executor had no way to know that `step1` was "Social security benefits" and `step4` was "Social assistance benefits".

### Fix

1. **Executor system prompt**: Added a CRITICAL instruction: "When writing the `friendly_response` field, ALWAYS refer to the actual field names from the plan — never use generic phrases like 'the first series' or 'the second value'."
2. **Execution prompt construction**: Added a step-to-field-name mapping block to the executor prompt. For each retrieve/retrieve_batch step, the field name is extracted from the step args and included as: `step1 → field: Social security benefits`
3. **Retrieve step labels**: Each retrieve step in the prompt now includes the field name: `step1 (field: Social security benefits): ALREADY DONE — value is 344.98`

**Files:** `backend/src/pipeline.py` — `build_executor_agent()` system prompt and execution prompt construction in `run_pipeline()`

### Verification

Query 6 now returns: *"The CAGR for Social security benefits from 2017 to 2021 is about 25.4%, while the CAGR for Social assistance benefits over the same period is about 21.0%."*

---

## Bug 14: `step_results` Empty in Executor Output

**Status:** Fixed (uncommitted)
**Date:** 2026-06-30
**Severity:** High

### Symptom

Most queries returned `"step_results": {}` in the execution output, even though the executor had successfully computed results. The frontend's "Calculation Steps" panel couldn't display intermediate values.

### Root Cause

The executor agent's system prompt instructed it to "Return the final answer as structured data" but never explicitly required populating `step_results`. The LLM focused on `final_answer` and `friendly_response`, leaving `step_results` as the default empty dict.

### Fix

Added a CRITICAL section to the executor system prompt:
```
### CRITICAL — step_results MUST be populated:
The `step_results` field in your output MUST contain an entry for EVERY step in the plan,
including pre-populated steps. For each step key (e.g. "step1", "step2"), set its value
to the result of that step. For retrieve/retrieve_batch steps, use the retrieved value(s).
For named operations and compute steps, use the computed result.
Example: if the plan has step1 (retrieve_batch), step2 (retrieve_batch), step3 (compute),
then step_results should be: {"step1": {...}, "step2": {...}, "step3": <computed_value>}.
Do NOT leave step_results as an empty dict {}.
```

**File:** `backend/src/pipeline.py` — `build_executor_agent()` system prompt

### Verification

All 10 test queries now return populated `step_results`:
- Q6: `{"step1": 344.98, "step2": 852.2, "step3": 0.2537, "step4": 4099.55, "step5": 8792.95, "step6": 0.2102}`
- Q7: `{"step1": "{2012: 69871.69, ...}", "step2": "{2012: 11054.95, ...}", "step3": 4420.2, "step4": 940.52, "step5": "Employers' social contributions"}`
- Q9: `{"step1": "{2015: 97217.31, ...}", "step2": 117408.64}`
- Q14: `{"step1": 51002.8, "step2": 35431.14, "step3": 15571.66}`

---

## Bug 15: Planner Returns `plan` as List Instead of Dict (Validation Error)

**Status:** Fixed (uncommitted)
**Date:** 2026-06-30
**Severity:** High

### Symptom

Query 8 ("Analyze the trend of interest expense from 2019 to 2023 and identify the year with the maximum value increase") failed with: `Exceeded maximum retries (3) for result validation` — `ValidationError: Input should be an object [type=dict_type, input_value=[...], input_type=list]`.

### Root Cause

The LLM sometimes returned the `plan` field as a JSON array of step objects instead of a dict keyed by step names. The `QueryPlan` model expected `dict[str, PlanStep]` with no tolerance for list input. After 3 retries, Pydantic AI raised `UnexpectedModelBehavior`.

### Fix

Added a `field_validator("plan", mode="before")` to `QueryPlan` that converts list input to dict:
- If `plan` is a list, each element is assigned a key `step1`, `step2`, etc.
- If an element has a `"step"` key containing a dict, that inner dict is used as the step value
- Also handles string input by attempting JSON parse first

```python
@field_validator("plan", mode="before")
@classmethod
def _parse_plan(cls, v):
    if isinstance(v, list):
        converted = {}
        for i, item in enumerate(v, 1):
            if isinstance(item, dict):
                if "step" in item and isinstance(item["step"], dict):
                    converted[f"step{i}"] = item["step"]
                else:
                    converted[f"step{i}"] = item
            else:
                converted[f"step{i}"] = item
        return converted
    ...
```

**File:** `backend/src/pipeline.py` — `QueryPlan` model

### Verification

Query 8 now succeeds: planner returns a valid dict plan, `retrieve_batch` is used for 5 years of Interest expense, executor computes year-over-year differences and identifies 2023 as the max increase year.

---

## Bug 16: New ChatGPT-Like UI Not Live on Deployed Site

**Status:** Resolved
**Date:** 2025-07-16
**Severity:** High

### Symptom

The deployed site at `https://excel-chat-production-76dc.up.railway.app` showed the old 2-tab interface ("Upload & Describe" / "Query") instead of the new sidebar + threads + conversation layout designed in `plans/ui-update.md`.

### Root Cause

The git repo root is at `capstone/` (not `capstone/excel-chat/`). The repo contains **two copies** of the frontend:
- `frontend/ragsheets/` (repo root level) — old 2-tab UI, last updated at commit `7078e85`
- `excel-chat/frontend/ragsheets/` — new sidebar + threads UI, updated at commit `2b35299`

Railway was configured with `rootDirectory: /` (repo root) and `dockerfilePath: backend/Dockerfile`. The Dockerfile's `COPY frontend/ragsheets/ ./` resolved to `capstone/frontend/ragsheets/` (the old copy), not `capstone/excel-chat/frontend/ragsheets/` (the new copy). So even though Railway deployed from the correct `agentic` branch with the correct commit, the Docker build context picked up the wrong frontend directory.

### Fix

Changed Railway's `rootDirectory` from `/` to `excel-chat/` via:
```bash
railway environment edit --service-config excel-chat source.rootDirectory "excel-chat"
```

This ensures the Dockerfile's `COPY frontend/ragsheets/ ./` resolves to `excel-chat/frontend/ragsheets/` — the directory with the new UI.

### Verification

Confirmed the new UI is live at `https://excel-chat-production-76dc.up.railway.app`:
- Left sidebar with "Sheets" section (upload + file listing) and "Threads" section (new thread button)
- Main content area: "Select a thread or create a new one to start asking questions."
- No "Upload & Describe" / "Query" tabs

---

## Bug 17: Some Queries Return 0 or Incorrect Results Despite Non-Zero Data

**Status:** Open
**Date:** 2025-07-16
**Severity:** High

### Symptom

When running queries through the full LLM pipeline, some questions return 0 or incorrect computed values even though the underlying DataFrame has non-zero data. The data-layer unit tests (`test_financial_questions.py`) all pass (56/56), confirming data retrieval and sandbox execution logic is correct in isolation. The bug manifests only in the LLM agent layer.

### Root Cause

Multiple contributing factors:

**1. Exact-match field name lookup (primary cause)**

`retrieve` in `tools.py:148` uses exact string matching against the DataFrame index:
```python
if field in df.index and year in df.columns:
    val = df.loc[field, year]
    result = str(float(val))
else:
    return f"ERROR: '{field}' or '{year}' not found in sheet '{sheet}'."
```

When the LLM planner generates a field name that doesn't exactly match the DataFrame index (e.g., "Capital expenditure" instead of "Capital", or "Interest" instead of "Interest expense"), `retrieve` returns an ERROR string. The executor LLM then receives this ERROR as a pre-populated value and may default to 0 in calculations.

The planner's system prompt lists all available fields, but LLMs still sometimes generate paraphrased or truncated field names, especially for long names like "Property expense other than interest" or "To residents other than government units".

**2. `-inf` values from `clean_dataframe` propagated to calculations**

`clean_dataframe` in `excelservices.py:235` fills NaN values with `-np.inf`:
```python
df = df.fillna(-np.inf).reset_index(drop=True, inplace=False)
```

When `retrieve` fetches a field/year that has `-inf`, it returns the string `"-inf"`. The executor LLM may:
- Fail to parse `"-inf"` as a number and default to 0
- Use `-inf` in calculations, producing `NaN` (e.g., `-inf * 0 = NaN`)
- Convert `NaN` to 0 in the final answer

The field "Consumption of fixed capital" has `-inf` for years 2011–2021 in the example dataset. Any query touching this field will hit this issue.

**3. `retrieve_batch` returns `null` for missing field/year combinations**

In `tools.py:265`, `retrieve_batch` returns `None` (JSON `null`) for years where the field is not found:
```python
else:
    result_obj[year] = None
```

When pre-populated and formatted for the executor prompt, this renders as:
```
step1: {"2018": 1500.0, "2019": null}
```

The executor LLM may interpret `null` as 0, producing incorrect averages, sums, or other aggregates.

**4. No field name normalization or fuzzy matching**

Neither `retrieve` nor `retrieve_batch` performs any case normalization, whitespace trimming, or fuzzy matching against the DataFrame index. A field name like "capital" (lowercase) or "Capital " (trailing space) will fail exact match even though "Capital" exists in the index.

### Proposed Fix

**A. Add fuzzy field name matching in `retrieve` and `retrieve_batch`:**
```python
def _normalize_field(field: str, df_index: pd.Index) -> str | None:
    """Match a field name case-insensitively, with whitespace normalization."""
    field_lower = field.strip().lower()
    for idx_name in df_index:
        if idx_name.strip().lower() == field_lower:
            return idx_name
    return None
```

Use this before the `field in df.index` check in both `retrieve` and `retrieve_batch`.

**B. Replace `-inf` with an error in retrieval results:**

In `retrieve`, check for `-inf` before converting to float:
```python
val = df.loc[field, year]
if np.isinf(val) or np.isnan(val):
    return f"ERROR: No data available for '{field}' in {year}."
result = str(float(val))
```

Same check in `retrieve_batch`.

**C. Filter out `null` values in `retrieve_batch` results:**

Instead of returning `null` for missing years, omit the key entirely or return an explicit error string for that year.

### Verification

- Run the full pipeline test (`test_query_pipeline.py`) with queries that previously returned 0
- Verify that queries involving "Consumption of fixed capital" no longer return 0 or NaN
- Test with paraphrased field names (e.g., "capital" instead of "Capital") to confirm fuzzy matching works
