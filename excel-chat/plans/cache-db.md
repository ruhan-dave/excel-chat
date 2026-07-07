# Cache & Database Architecture

## Current Storage Architecture

| Tier | Technology | Location | Purpose |
|------|-----------|----------|---------|
| File storage | AWS S3 (`ragsheets`) | Cloud | Uploaded Excel files (auto-expire 90 days) |
| Metadata + cache | SQLite (`backend/src/sheets.db`) | Local disk | Sheet metadata, descriptions, LLM response cache |
| Embeddings (legacy) | ChromaDB (`./db/`) | Local disk | TF-IDF embeddings — **disabled, code commented out** |

### What's Stored Where

- **S3**: Raw `.xlsx`/`.xls` files under `uploads/{file_id}/{filename}`
- **SQLite `files` table**: `file_id`, `file_name`, `s3_key`, `sheet_count`, `created_at`
- **SQLite `sheets` table**: `sheet_id`, `file_id`, `sheet_name`, `fields_json`, `years_json`, `schema_group`, `user_description`, `auto_description`, `row_count`
- **SQLite `llm_cache` table**: `cache_key` (SHA-256 of `model:prompt`), `response`, `model`, `created_at`, `last_accessed`, `hit_count`

---

## Already Implemented

### 1. Request-Response Caching (Exact Match)
- SQLite `llm_cache` table with SHA-256 key from `model:prompt`
- `get_cached_response()` / `set_cached_response()` in `sheet_metadata.py`
- Wired into `auto_describe_sheet()` — identical sheet profiles skip the LLM call
- LRU eviction via `cleanup_old_cache_entries(max_age_days=30)`
- API: `GET /cache/stats`, `POST /cache/cleanup`

### 2. Idempotency Checks
- `auto_describe_sheet()` returns immediately if `sheet.auto_description` already exists
- `auto_describe_all_sheets()` skips sheets that already have descriptions
- Re-uploading the same file does not trigger duplicate LLM calls

### 3. Storage Monitoring
- `GET /storage/stats` returns counts across all tiers (S3 files, sheets, cache entries, cache hits)
- ChromaDB monitoring removed (code commented out, no longer used)

---

## Implemented: Redis (Upstash) for LLM Cache

### Problem
SQLite is single-writer and file-locked. Under concurrent user load, cache reads/writes serialize and become a bottleneck. SQLite also doesn't support TTL natively.

### Architecture

```
User Query → Cache Lookup (Redis) → Hit? → Return cached response
                                  → Miss? → Call LLM → Store in Redis → Return
```

### Implementation Steps

1. **Provision Redis**
   - Dev: `redis-server` locally or Docker container (`redis:7-alpine`)
   - Prod: **Upstash Redis** (3rd-party serverless Redis, not AWS). Free tier: 500K commands/month, 256MB storage, no credit card required. Pay-per-request beyond that.
   - Connection: `REDIS_URL` env var (e.g., `redis://localhost:6379/0` for dev, `rediss://default:xxx@us1-xxx.upstash.io:6379` for Upstash)
   - Upstash also provides a REST API for environments that can't open TCP connections (Cloudflare Workers, Vercel Edge)

2. **Add `redis` to `requirements.txt`**
   ```
   redis>=5.0.0
   ```

3. **Create `cache_service.py`** — thin wrapper with fallback
   ```python
   import os, json, hashlib
   import redis

   _redis_client = None

   def _get_redis():
       global _redis_client
       if _redis_client is None:
           url = os.environ.get("REDIS_URL")
           if url:
               _redis_client = redis.from_url(url, decode_responses=True)
       return _redis_client

   def cache_get(model: str, prompt: str) -> str | None:
       key = f"llm:{hashlib.sha256(f'{model}:{prompt}'.encode()).hexdigest()}"
       r = _get_redis()
       if r:
           val = r.get(key)
           if val:
               r.incr(f"{key}:hits")
               r.expire(key, 604800)  # refresh 7-day TTL on access
           return val
       # Fallback to SQLite
       from sheet_metadata import get_cached_response
       return get_cached_response(model, prompt)

   def cache_set(model: str, prompt: str, response: str, ttl: int = 604800):  # 7 days
       key = f"llm:{hashlib.sha256(f'{model}:{prompt}'.encode()).hexdigest()}"
       r = _get_redis()
       if r:
           r.setex(key, ttl, response)
       else:
           from sheet_metadata import set_cached_response
           set_cached_response(model, prompt, response)
   ```

4. **Update `excelservices.py`** — replace direct SQLite cache calls with `cache_service`
   - `auto_describe_sheet()` calls `cache_get()` / `cache_set()` instead of SQLite directly
   - Fallback to SQLite if Redis unavailable (graceful degradation)

5. **TTL Strategy**
   - All cache types: 7 days (`ttl=604800`) — see TTL section below for rationale

6. **Migration path**
   - Phase 1: Redis with SQLite fallback (no downtime)
   - Phase 2: Redis-only (remove SQLite cache table after verification)

7. **Testing**
   - Unit test: `cache_get`/`cache_set` with mock Redis
   - Integration test: start Redis container, verify set/get/TTL expiry
   - Fallback test: kill Redis, verify SQLite fallback works

### Does Redis Cost Money?

**Upstash** is a 3rd-party serverless Redis platform (not an AWS service). We use it because:
- **Free forever for hobbyists**: 256MB max data size, 500K commands/month, 10GB monthly bandwidth, no credit card required
- Pay-per-request pricing beyond free tier — $0 when idle, $0.20 per 100K commands + $0.25/GB-month storage
- Fixed plans available for high steady traffic ($10/mo for 250MB with unlimited commands, up to $800/mo for 100GB)
- No infrastructure to manage — just a connection URL
- Native TTL support (7-day auto-expiry)
- Supports RediSearch (vector similarity) and RedisJSON modules — needed for semantic cache KNN queries
- REST API available for serverless/edge environments that can't open TCP connections

| Environment | Provider | Cost |
|-----------|----------|------|
| **Dev** | Local Docker (`redis:7-alpine`) | $0 |
| **Prod** | Upstash free tier | $0 (256MB, 500K commands/mo, 10GB bandwidth) |
| **Prod (scaled)** | Upstash pay-as-you-go | $0.20 per 100K commands + $0.25/GB-mo storage |
| **Prod (high traffic)** | Upstash fixed plan | $10–$800/mo (flat, unlimited commands) |

**Not using**: AWS ElastiCache (no indefinite free tier, ~$12/mo minimum for `cache.t4g.micro`, always-on node-hour billing). AWS offers only a 12-month trial with 750 hours of `cache.t3.micro`.

**Other providers considered**:
- **Aiven**: 1GB free tier but runs Valkey (no RediSearch module — can't do vector search)
- **Redis Cloud**: 30MB free tier with RediSearch, but too small for production cache
- **Railway**: Self-deployed Redis, no managed vector search

### TTL & Auto-Deletion of Cache

Redis has **native TTL support** — every key can have an expiration time. After TTL expires, Redis automatically deletes the key. No cron job or manual cleanup needed.

```python
# Set key with 7-day TTL — Redis auto-deletes after 7 days
r.setex("llm:abc123", 604800, response)

# Refresh TTL on cache hit (sliding expiration — 7 days from last access)
r.expire("llm:abc123", 604800)
```

**TTL: 7 days for all cache types**

Simplifies configuration — one TTL for everything. Rationale:
- Auto-descriptions: stable, 7 days is conservative
- Query responses: if user uploads new data within 7 days, cache may be stale, but the cost of a stale cache hit (wrong answer) is lower than the cost of redundant LLM calls. The pipeline re-retrieves from live DataFrames each query, so only the LLM classification/plan is cached — not the actual data lookup.
- If a user uploads new sheets or updates descriptions, we invalidate their cache explicitly (see cache invalidation in semantic caching section).

**Current SQLite fallback**: No native TTL. `cleanup_old_cache_entries(max_age_days=7)` must be called via cron. The `POST /cache/cleanup` endpoint exists for this.

**With Redis**: TTL is automatic. `cleanup_old_cache_entries()` becomes a no-op fallback only.

### Cost Estimate
- Upstash free tier: $0 — "Free forever for hobbyists" (256MB data, 500K commands/mo, 10GB bandwidth — covers ~33K–100K queries/month depending on cache hit rate, each query generates 5–15 cache operations)
- Upstash pay-as-you-go: $0.20 per 100K commands + $0.25/GB-month storage beyond free tier
- Upstash fixed plans: $10/mo (250MB, unlimited commands) for steady low traffic, $20/mo (1GB) for moderate traffic
- Local Docker (dev): $0

---

## Planned: S3 Lifecycle Policies

### Problem
Uploaded Excel files accumulate indefinitely. Users upload files, stop using the app, and files persist forever in S3.

### Architecture

```
S3 Bucket (ragsheets)
├── uploads/          → Standard (0-30 days)
├── uploads/          → Standard-IA (30-90 days, cheaper storage)
└── uploads/          → Expire (90+ days, auto-delete)
```

### Retention Period & Deletion Guarantee

**How many days before auto-deletion?**
- **30 days**: Files transition to Standard-IA (cheaper storage, still accessible)
- **90 days**: Files are **permanently deleted** by S3 lifecycle policy
- This is configurable via the lifecycle JSON (`Expiration.Days`)

**Can we guarantee deletion to avoid charges?**
- **Yes.** S3 lifecycle `Expiration` rules are enforced by AWS at the infrastructure level — not application code. Once configured, AWS guarantees deletion regardless of application state.
- After deletion, you stop paying for that object immediately (billed per-GB-hour, no partial month refund, but no future charges).
- **S3 also charges for incomplete multipart uploads** — add a cleanup rule:
  ```json
  {"ID": "cleanup-incomplete", "Filter": {"Prefix": "uploads/"}, "Status": "Enabled",
   "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1}}
  ```
### SQLite Metadata & Cron Jobs

S3 lifecycle handles object deletion, but SQLite metadata in `sheets.db` is not automatically cleaned. Two cron jobs are needed:

#### What's in SQLite (`backend/src/sheets.db`)?

| Table | Contents | Grows When | Cleaned By |
|-------|----------|-----------|------------|
| `files` | One row per uploaded file (file_id, file_name, s3_key, sheet_count, created_at) | User uploads a file | Cron job #1 (after S3 expires the object) |
| `sheets` | One row per sheet (sheet_id, file_id, sheet_name, fields, years, schema_group, descriptions, row_count) | User uploads a multi-sheet file | Cascades with `files` delete via `ON DELETE CASCADE` |
| `llm_cache` | Cached LLM responses (cache_key, response, model, created_at, last_accessed, hit_count) | Every LLM call that misses cache | Cron job #2 (entries older than 7 days) |

#### Cron Job #1: Orphaned File Metadata Cleanup (daily)

When S3 lifecycle deletes an object at day 90, the SQLite `files` and `sheets` rows become orphaned — they reference an S3 key that no longer exists. This cron job cleans them up.

**Option A: S3 event notification (recommended for prod)**
```
S3 ObjectRemoved event → SQS queue → Lambda function
  → Lambda calls DELETE /files/{file_id}
  → delete_file() removes sheets rows + files row from SQLite
```

**Option B: Scheduled SQL cleanup (simpler, for dev/staging)**
```sql
-- Run daily via cron or EventBridge → Lambda
DELETE FROM sheets WHERE file_id IN (
  SELECT file_id FROM files
  WHERE created_at < datetime('now', '-90 days')
);
DELETE FROM files
WHERE created_at < datetime('now', '-90 days');
```

**Option C: Application-level cron (no AWS setup needed)**
```python
# Add to main.py — runs on startup or via APScheduler
import asyncio
from datetime import datetime

async def daily_cleanup():
    """Delete SQLite metadata for files older than 90 days."""
    conn = _get_db()
    old_files = conn.execute(
        "SELECT file_id FROM files WHERE created_at < datetime('now', '-90 days')"
    ).fetchall()
    for f in old_files:
        delete_file(f["file_id"])
    conn.close()
    print(f"Cleaned {len(old_files)} orphaned file records")
```

#### Cron Job #2: LLM Cache Cleanup (daily)

With Redis: **not needed** — Redis TTL (7 days) auto-expires keys.

With SQLite fallback only: run daily to delete cache entries not accessed in 7 days.
```bash
# Via curl to the existing endpoint
curl -X POST http://localhost:8000/api/cache/cleanup?max_age_days=7
```
Or via SQL directly:
```sql
DELETE FROM llm_cache WHERE last_accessed < datetime('now', '-7 days');
```

#### Cron Schedule Summary

| Job | Frequency | With Redis | Without Redis |
|-----|-----------|-----------|---------------|
| Orphaned file metadata | Daily at 3 AM | Required | Required |
| LLM cache cleanup | Daily at 4 AM | Not needed (TTL handles it) | Required (7-day eviction) |
| Incomplete multipart upload cleanup | Daily | S3 lifecycle handles it | S3 lifecycle handles it |

### Implementation Steps

1. **S3 Bucket Lifecycle Rule** (AWS Console or CLI)
   ```bash
   aws s3api put-bucket-lifecycle-configuration \
     --bucket ragsheets \
     --lifecycle-configuration file://lifecycle.json
   ```
   ```json
   {
     "Rules": [
       {
         "ID": "expire-uploads-90d",
         "Filter": {"Prefix": "uploads/"},
         "Status": "Enabled",
         "Transitions": [
           {"Days": 30, "StorageClass": "STANDARD_IA"}
         ],
         "Expiration": {"Days": 90}
       }
     ]
   }
   ```

2. **Cascade cleanup on expiration**
   - S3 event notification → Lambda/SQS → call `DELETE /files/{file_id}`
   - Or: scheduled cron job (EventBridge → Lambda) that:
     - Lists S3 objects with `LastModified > 90 days`
     - Calls `delete_file()` to clean SQLite metadata + S3 object

3. **User-facing cleanup**
   - Add `last_accessed` column to `files` table
   - Update `last_accessed` when any sheet from that file is queried
   - `/storage/stats` shows files not accessed in 30/60/90 days
   - Frontend: "Stale files" warning in Sheet Manager UI

4. **Soft delete before hard delete**
   - Day 85: email/notification to user "Your file X will be deleted in 5 days"
   - Day 90: S3 lifecycle expires the object
   - Day 90: cron job cleans SQLite metadata for orphaned file_ids

5. **Testing**
   - Verify lifecycle rule applied via `aws s3api get-bucket-lifecycle-configuration`
   - Test cron job locally with mock S3 (moto library)
   - Verify `delete_file()` cleans both S3 + SQLite

### Cost Impact
- Standard: $0.023/GB-month → Standard-IA: $0.0125/GB-month (46% savings)
- Expiration at 90d eliminates long-tail storage costs entirely
- For 100 users uploading 5MB files weekly: saves ~$2/mo (scales linearly)

---

## Implemented: Semantic Caching (Per-User)

### Problem
Exact-match cache misses semantically equivalent queries:
- "What was revenue in 2022?" vs "2022 revenue" vs "Show me revenue for FY2022"
- Each triggers a full LLM call despite being identical in intent.
- Without semantic matching, paraphrased queries waste tokens re-running the entire pipeline (planner + executor + sandbox) even though the answer is identical to a previous query.

### Architecture

```
User Query
  → Embed query with all-MiniLM-L6-v2 (384-dim, ~5-20ms on CPU)
  → Search user's cache namespace for cosine similarity >= 0.88
  → Hit (sim >= 0.88)? → Return cached full response (skip entire pipeline)
  → Miss? → Run full pipeline → Store {embedding, query, response} in user's namespace
```

### Why `all-MiniLM-L6-v2` Instead of `Qwen/Qwen3-Embedding-8B`

The system previously used `Qwen/Qwen3-Embedding-8B` (1024-dim, ~16GB), which provides superior semantic accuracy. It was switched to `all-MiniLM-L6-v2` (384-dim, ~80MB) for the following reasons:

| Factor | Qwen3-8B | MiniLM |
|--------|----------|--------|
| **Model size** | ~16GB download | ~80MB download |
| **Inference latency** | ~200-500ms (CPU), ~20-50ms (GPU) | ~5-20ms (CPU) |
| **Container image** | +16GB — bloated, slow to build/deploy | +80MB — negligible |
| **Deployment reliability** | Failed to load in container environment | Loads reliably, pre-downloaded in Dockerfile |
| **Embedding quality** | Best (1024-dim, nuanced semantic matching) | Good (384-dim, sufficient for paraphrase matching) |
| **Threshold** | ~0.92 (tighter embeddings need higher bar) | 0.88 (wider embeddings need lower bar to catch paraphrases) |
| **Cost** | Free (open-source) but needs GPU for production | Free, runs on CPU |

**The trade-off**: MiniLM is less semantically precise — it may miss some paraphrases that Qwen3 would catch, or produce false positives at the edges. For financial query paraphrase matching (e.g., "Revenue in 2022" ~ "2022 revenue" ~ "How much revenue did we make in 2022"), MiniLM at threshold 0.88 is sufficient. The embedding call happens once per query and costs ~5-20ms — negligible compared to the 1-22s pipeline it skips on a cache hit.

**Upgrade path**: Switch back to `Qwen/Qwen3-Embedding-8B` when GPU inference is available. Alternatively, use a hosted embedding API (Hugging Face Inference API, Together AI, or OpenAI `text-embedding-3-small`) to avoid the 16GB local download entirely — the embedding call is a single HTTP request per query, and ~100-300ms API latency is acceptable relative to the pipeline savings. See "Embedding API Options" below.

**Critical**: Cache is **per-user** to prevent context distillation. User A's "revenue" query should not return User B's cached response (different sheets, different data).

### Implementation Steps

1. **Add user identity to the system**
   - Add `user_id` column to `files`, `sheets`, and `llm_cache` tables
   - `user_id` passed from frontend (auth token, session ID, or API key)
   - All queries scoped: `WHERE user_id = ?`

2. **Embedding model for cache keys**
   - **Implemented with**: `sentence-transformers/all-MiniLM-L6-v2` (384-dim, ~80MB)
   - **Previously used**: `Qwen/Qwen3-Embedding-8B` (1024-dim, ~16GB) — more accurate semantic matching, but switched to MiniLM because Qwen3-8B was too heavy for the container image and failed to load reliably in the deployment environment
   - MiniLM is lightweight, fast on CPU, and provides sufficient semantic quality for financial query paraphrase matching at threshold 0.88
   - Qwen3-8B remains the preferred model for accuracy — switch back when GPU inference is available in the deployment environment
   - 384 dimensions → ~1.5KB per cache entry (vs ~4KB for Qwen3)
   - Usage:
     ```python
     from sentence_transformers import SentenceTransformer
     model = SentenceTransformer("all-MiniLM-L6-v2")
     embeddings = model.encode(sentences, normalize_embeddings=True)
     ```
   - **Upgrade path**: Switch back to `Qwen/Qwen3-Embedding-8B` (1024-dim, best quality) when GPU inference is available. Update `_MODEL_NAME`, `_EMBED_DIM` in `semantic_cache.py`, adjust threshold from 0.88 to ~0.92 (Qwen3 produces tighter embeddings), and update the Dockerfile pre-download step.

3. **Add `sentence-transformers` to requirements**
   ```
   sentence-transformers>=2.5.0
   ```

4. **Create `semantic_cache.py`**
   ```python
   import numpy as np
   from sentence_transformers import SentenceTransformer

   _model = None

   def _get_model():
       global _model
       if _model is None:
           _model = SentenceTransformer("all-MiniLM-L6-v2")  # switched from Qwen3-8B (too heavy)
       return _model

   def embed_query(text: str) -> np.ndarray:
       return _get_model().encode(text, normalize_embeddings=True)

   def find_similar_cached(
       user_id: str,
       query_embedding: np.ndarray,
       threshold: float = 0.92,
       top_k: int = 1,
   ) -> str | None:
       """Search user's cached queries by cosine similarity."""
       # Pull user's cache entries from storage
       # Compare query_embedding against each cached embedding
       # Return best match if sim > threshold
       ...

   def store_cached(
       user_id: str,
       query: str,
       query_embedding: np.ndarray,
       response: str,
       model: str,
   ):
       """Store query + embedding + response in user's cache namespace."""
       ...
   ```

5. **Storage: Redis with vector search**
   - Use Redis Stack (RediSearch module) for vector similarity search
   - Key format: `semantic_cache:{user_id}:{sha256(query)}`
   - Store: `{embedding: [0.1, ...], query: "...", response: "...", model: "..."}`
   - Index: `FT.CREATE idx ON JSON PREFIX 1 semantic_cache: SCHEMA $.embedding VECTOR FLAT 6 TYPE FLOAT32 DIM 384 DISTANCE_METRIC COSINE`
   - Query: `FT.SEARCH idx "*=>[KNN 1 @embedding $vec]" PARAMS 2 vec $blob SORTBY __score`

6. **Fallback: SQLite + numpy (no Redis)**
   - Store embeddings as JSON in `llm_cache` table (`embedding_json` column)
   - On lookup: load all user's embeddings, compute cosine similarity in numpy
   - Works for <10K cache entries per user; switch to Redis for scale

7. **Threshold tuning**
   - Implemented at 0.88 for MiniLM (balances catching paraphrases vs avoiding false positives)
   - If using Qwen3-8B: use ~0.92 (tighter embeddings need higher threshold)
   - Log cache hit similarity scores to monitor
   - A/B test: 0.85 vs 0.88 vs 0.92 — measure user satisfaction with cached responses
   - Add `cache_threshold` config to adjust without redeploy

8. **Per-user isolation**
   - Every cache lookup includes `user_id` in the filter
   - Redis: `FT.SEARCH` with `FILTER user_id '{user_id}'`
   - SQLite: `WHERE user_id = ?` in every query
   - No cross-user cache leakage, ever

9. **Cache invalidation**
   - When user uploads a new file or updates a sheet description:
     - Invalidate all semantic cache entries for that user
     - `DELETE FROM llm_cache WHERE user_id = ?` or `DEL semantic_cache:{user_id}:*`
   - When user deletes a file:
     - Same invalidation
   - TTL: 7 days for all cache types (consistent with Redis plan above)

10. **Monitoring**
    - `GET /cache/stats?user_id=X` — per-user hit rate, entry count
    - Log: cache hit/miss, similarity score, user_id (for debugging, not PII)
    - Alert: if cache hit rate < 10%, threshold may be too high

### Testing
- Unit: `embed_query()` returns 384-dim normalized vector
- Unit: `find_similar_cached()` returns correct match for paraphrased queries
- Unit: `find_similar_cached()` returns None for unrelated queries
- Integration: two users with same query but different sheets → different responses
- Integration: cache invalidation on file upload → subsequent query misses cache
- Performance: cache lookup < 50ms for 1K entries (numpy), < 5ms (Redis)

### Cost Estimate
- `all-MiniLM-L6-v2`: free, open-source, runs on CPU (~80MB model, fast inference)
- Model size: ~80MB download (one-time), pre-downloaded in Dockerfile for zero cold-start
- Redis Stack: same as Redis plan above (384-dim vectors = ~1.5KB per cache entry)
- Savings: if 30% of queries are semantic duplicates → 30% fewer LLM calls
- At $0.001/call (deepseek-v4-flash): 1K queries/day → saves ~$10/mo

### Embedding API Options (Alternative to Local MiniLM)

If semantic quality at threshold 0.88 is insufficient (too many false negatives — paraphrases that don't hit the cache), the embedding model can be swapped to a hosted API without loading a large model locally. The embedding call happens **once per query** (~5-20ms for MiniLM), so even ~100-300ms API latency is acceptable relative to the 1-22s pipeline it skips on a cache hit.

| Provider | Model | Dimensions | Latency | Cost | Notes |
|----------|-------|-----------|---------|------|-------|
| **Hugging Face Inference** | `Qwen/Qwen3-Embedding-8B` | 1024 | ~100-300ms | Free tier, then ~$0.01/1K | Best option for Qwen3 quality without local download |
| **Together AI** | Various embedding models | Varies | ~50-150ms | ~$0.01/1K | Check Qwen3 availability |
| **OpenAI** | `text-embedding-3-small` | 1536 | ~50-100ms | $0.02/1M tokens | Very reliable, not Qwen3 but high quality |
| **OpenAI** | `text-embedding-3-large` | 3072 | ~50-100ms | $0.13/1M tokens | Best quality, higher cost |
| **Jina AI** | `jina-embeddings-v3` | 1024 | ~50-100ms | Free tier available | Competitive with Qwen3, specialized in embeddings |

**Implementation change is isolated to `semantic_cache.py`:**

```python
# Current (local MiniLM):
_model = SentenceTransformer("all-MiniLM-L6-v2")
vec = _model.encode(text, normalize_embeddings=True)

# API-based (e.g. Hugging Face):
import httpx
resp = httpx.post(
    "https://api-inference.huggingface.co/pipeline/feature-extraction/Qwen/Qwen3-Embedding-8B",
    headers={"Authorization": f"Bearer {HF_TOKEN}"},
    json={"inputs": text, "options": {"wait_for_model": True}},
    timeout=10.0,
)
vec = np.array(resp.json(), dtype=np.float32)
vec = vec / np.linalg.norm(vec)  # L2-normalize for cosine similarity
```

The rest of the system (Redis vector search, SQLite fallback, threshold comparison) stays the same — only the embedding generation changes. When switching to a 1024-dim model, update `_EMBED_DIM` and the Redis vector index `DIM` parameter.

---

## Caching: Intermediate Result & Query Cache

### Full Caching Layer Overview

The system has **five distinct caching layers**, each catching a different type of redundancy. They work together — a query that misses one layer may hit another:

| # | Layer | What It Caches | Granularity | Scope | Key Format | Storage |
|---|-------|---------------|------------|-------|------------|---------|
| 1 | **Exact-match LLM cache** | Sheet auto-description LLM responses | Per prompt | Global | `sha256(model:prompt)` | Redis `llm:{hash}` / SQLite `llm_cache` |
| 2 | **Semantic cache** | Full query responses (entire pipeline output) | Per query | Per-user | `semantic:{user_id}:{hash}` + 384-dim embedding | Redis hash / SQLite `llm_cache` with `embedding_json` |
| 3 | **Result cache — retrieve** | Individual `df.loc[field, year]` values | Per retrieve call | Per-user | `result:{uid}:revenue_2022` | Redis / SQLite `result_cache` |
| 4 | **Result cache — sandbox** | `execute_python_code()` output | Per code block | Per-user | `sandbox:{uid}:{sha256(code)}` | Redis / SQLite `result_cache` |
| 5 | **Result cache — structured** | Named operation results (post-execution) | Per plan step | Per-user | `result:{uid}:sum_revenue_2022_2023_2024_2025` | Redis / SQLite `result_cache` |

**How the layers interact:**

```
User asks: "What is the ratio of revenue to grants for 2022-2025?"
  |
  +-- Layer 2 (Semantic cache): Embed query -> cosine similarity vs past queries
  |    HIT (>=0.88)? -> Return full cached response. DONE (skip entire pipeline).
  |    MISS -> continue to pipeline
  |
  +-- Pipeline starts: Planner generates QueryPlan
  |
  +-- Pre-population: retrieve_batch("Revenue", ["2022","2023","2024","2025"])
  |    +-- Layer 3 (retrieve cache): check result:{uid}:revenue_2022_2023_2024_2025
  |    |    HIT? -> return cached values, skip DataFrame scan
  |    |    MISS? -> scan DataFrame, store for future use
  |    +-- Same for grants
  |
  +-- Executor: generate Python code for ratio calculation
  |    +-- Layer 4 (sandbox cache): check sandbox:{uid}:{sha256(code)}
  |         HIT? -> return cached output, skip sandbox execution
  |         MISS? -> execute in sandbox, store for future use
  |
  +-- Post-execution: Layer 5 (structured keys)
  |    Derive canonical key: ratio_revenue_2022_2023_2024_2025_grants_2022_2023_2024_2025
  |    Store computed ratio for future queries that produce the same key
  |
  +-- Store full response in Layer 2 (semantic cache) for future paraphrase matches
```

**What each layer catches that others don't:**

| Scenario | Which Layer Catches It | Why Other Layers Miss |
|----------|----------------------|---------------------|
| Same sheet uploaded again, auto-describe runs | Layer 1 (exact-match) | Same prompt = same hash |
| "Revenue in 2022" asked twice | Layer 2 (semantic) | Identical query -> high cosine sim |
| "Revenue in 2022" then "2022 revenue" | Layer 2 (semantic) | Paraphrase -> cosine sim >= 0.88 |
| "Total revenue 2022-2025" then "Ratio of revenue to grants 2022-2025" | Layer 3 (retrieve) | Different queries (Layer 2 miss), but revenue_2022_2023_2024_2025 key is the same |
| Same CAGR formula re-computed with same values | Layer 4 (sandbox) | Different query (Layer 2 miss), same code hash |
| "Sum of revenue 2022-2025" then "Total revenue 2022 through 2025" | Layer 5 (structured) | Different queries (Layer 2 may miss), but `sum_revenue_2022_2023_2024_2025` key matches |

### Implementation Status: All Layers Implemented

All five caching layers are implemented and running in production:

| Layer | File | Status | Key Functions |
|-------|------|--------|--------------|
| 1 — Exact-match | `sheet_metadata.py`, `cache_service.py` | Implemented | `get_cached_response()`, `set_cached_response()` |
| 2 — Semantic | `semantic_cache.py` | Implemented | `embed_query()`, `semantic_lookup()`, `semantic_store()` |
| 3 — Retrieve cache | `result_cache.py` | Implemented | `result_cache_get()`, `result_cache_set()` |
| 4 — Sandbox cache | `result_cache.py` | Implemented | `sandbox_cache_get()`, `sandbox_cache_set()` |
| 5 — Structured keys | `result_cache.py` | Implemented | `cache_step_results()` (post-execution) |

**Invalidation**: On file upload or delete, `invalidate_user_cache(user_id)` clears all per-user layers (2-5) for that user. Layer 1 (global exact-match) is not user-scoped and persists. TTL: 7 days for all cache types.

### Intermediate Result Cache Details

Cache **individual computed values** (retrievals and calculations) so later queries can pull them without re-retrieving or re-computing.

#### Architecture: Where Caching Happens

The executor is an **LLM agent** (`build_executor_agent`) that calls tools (`retrieve`, `execute_python_code`) via Pydantic AI's `run_sync`. There is no deterministic step loop to intercept. Caching must happen at three points:

```
Query arrives
  │
  ▼
Planner agent → QueryPlan (steps)
  │
  ▼
Executor agent calls tools:
  ┌─────────────────────────────────────────┐
  │ retrieve(field, year, sheet?)           │  ← Cache Point A: inside tool
  │   → check result:{uid}:{key}            │
  │   → hit: return cached, skip DataFrame  │
  │   → miss: scan DataFrame, store result  │
  ├─────────────────────────────────────────┤
  │ execute_python_code(code)               │  ← Cache Point B: inside tool
  │   → hash code string                    │
  │   → hit: return cached output           │
  │   → miss: run sandbox, store result     │
  └─────────────────────────────────────────┘
  │
  ▼
Executor returns ExecutionResult
  │
  ▼
Post-execution: parse QueryPlan + step_results  ← Cache Point C
  → derive structured keys for each plan step
  → store with canonical keys for future queries
  │
  ▼
Responder agent → friendly response
  │
  ▼
Semantic cache: store full query+response (existing, unchanged)
```

**Cache Point A** (retrieve tool) is the highest-impact, simplest change. Most repeated work across queries is re-fetching the same field/year values.

**Cache Point B** (execute_python_code) caches sandbox execution results. The code string includes literal values from prior retrievals, so identical queries produce identical code. Works for named operations the agent computes via sandbox.

**Cache Point C** (post-execution) derives canonical structured keys from the `QueryPlan` and stores each step result under that key. This is what makes future queries hit the cache — the next planner may produce different step names, but the same `revenue_2022` key.

#### Structured Key Format

Every intermediate result gets a deterministic key encoding the operation, fields, and years:

```
{FIELD_YEAR}                                    — raw retrieval (single field, single year)
{FIELD_YEAR1_YEAR2_...}                         — raw retrieval (single field, multiple years)
{SHEET_FIELD_YEAR}                              — sheet-scoped retrieval
{OP_FIELD_YEAR}                                 — unary operation on a field/year
{OP_FIELD1_YEAR1_FIELD2_YEAR2}                  — binary operation across two fields
{OP_FIELD_YEAR1_YEAR2}                          — n-ary operation on one field across years
{OP_FIELD1_YEAR_FIELD2_YEAR}                    — binary operation, same year, two fields
```

**Key building rules**:
- Fields: lowercased, spaces → underscores, stripped of special chars (`Wages and salaries` → `wages_and_salaries`)
- Years: sorted ascending, kept as-is from DataFrame columns (handles `FY2022`, `2022-2023`)
- Sheet name: prefixed with `.` separator when present (`sheetA.revenue_2022`)
- Operation: canonical name from the mapping table below
- Delimiter: all parts joined with `_` — but to prevent collision (see Edge Cases), years are prefixed with `y` when ambiguous: `sum_revenue_y2022_y2023` vs `sum_revenue_2022_2023` (field named "2022")

**Collision prevention**: If a field name contains only digits (unlikely but possible), prefix years with `y` to disambiguate. Normal case: no prefix needed.

Examples:

| Key | Value | Meaning |
|-----|-------|---------|
| `revenue_2022` | 32500 | Raw retrieval of revenue for 2022 |
| `revenue_2022_2023_2024_2025` | `[32500, 34000, 35500, 37000]` | Raw retrieval of revenue across 4 years |
| `sheetA.revenue_2022` | 32500 | Revenue from sheetA only, 2022 |
| `sum_revenue_2022_2023_2024_2025` | 139000 | Sum of revenue 2022–2025 |
| `division_expense_grant_2022` | 4.27 | Expense / grant ratio for 2022 |
| `ratio_revenue_grant_2022_2023_2024_2025` | 2.34 | Revenue / grant ratio across 2022–2025 |
| `yoy_growth_revenue_2023_2024` | 4.4 | Year-over-year revenue growth, 2023→2024 |
| `return_percentage_wages_expense_2022` | 62.5 | Wages as % of expense, 2022 |

#### Operation Name Mapping

The pipeline's `NAMED_OPERATIONS` uses code names. Cache keys use canonical names that are more readable and match user language:

| Code name (`NAMED_OPERATIONS`) | Cache canonical | Aliases (user English) |
|--------------------------------|-----------------|----------------------|
| `add` | `sum` | total, summation, grand_total, add_up, aggregate |
| `divide` | `division` | divided, per |
| `multiply` | `multiply` | multiplication, times, product |
| `subtract` | `subtract` | difference, minus, less |
| `ratio` | `ratio` | ratio_of |
| `return_percentage` | `return_percentage` | percentage, percent, as_percent_of |
| `yoy_growth` | `yoy_growth` | year_over_year, annual_growth, growth_rate |
| `percentage_change` | `percentage_change` | pct_change, relative_change |
| `cagr` | `cagr` | compound_annual_growth, compound_growth |
| `average` | `average` | mean, avg |
| `median` | `median` | middle_value |
| `max` | `max` | maximum, highest, peak |
| `min` | `min` | minimum, lowest, bottom |
| `stdev` | `stdev` | standard_deviation, std_dev |
| `sqrt` | `sqrt` | square_root |
| `power` | `power` | exponent, squared, cubed |
| `log` | `log` | logarithm, ln |
| `abs` | `abs` | absolute, absolute_value |
| `negate` | `negate` | negative, opposite |
| `exp` | `exp` | exponential |

**Note**: `divide` and `ratio` are mathematically identical (a/b) but semantically distinct in the planner. Both map to separate canonical names to preserve the planner's intent. `return_percentage` is a/b*100 — different formula, different canonical name.

The alias table is used by the **semantic alias layer** (below) to match plain-English `compute` descriptions to cached keys.

#### Three-Layer Lookup

**Layer 1 — Retrieve tool cache** (inside `retrieve()` function):

Before scanning the DataFrame, check the cache:

```python
def retrieve(ctx, field, year, sheet=""):
    cache_key = build_retrieve_key(field, year, sheet)
    cached = result_cache_get(ctx.deps.user_id, cache_key)
    if cached is not None:
        return cached  # skip DataFrame scan entirely
    # ... existing DataFrame scan ...
    result = ...  # original return value
    result_cache_set(ctx.deps.user_id, cache_key, result)
    return result
```

Key: `result:{user_id}:{field}_{year}` or `result:{user_id}:{sheet}.{field}_{year}`
Value: the return string (e.g., `"32500"` or `"SheetA: 32500; SheetB: 31000"`)
TTL: 7 days

**Layer 2 — Sandbox cache** (inside `execute_python_code()` function):

Hash the code string and cache the output:

```python
def execute_python_code(ctx, code):
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    cached = sandbox_cache_get(ctx.deps.user_id, code_hash)
    if cached is not None:
        return cached
    result = ...  # run sandbox
    sandbox_cache_set(ctx.deps.user_id, code_hash, result)
    return result
```

Key: `sandbox:{user_id}:{code_hash}`
Value: the sandbox output string
TTL: 7 days

**Why this works**: The executor agent generates Python code with literal values from prior `retrieve` calls. If the same values are retrieved (because retrieve is cached), the agent generates the same code, producing the same hash. Named operations (`add`, `divide`, etc.) computed via sandbox are automatically cached.

**Why this might miss**: The LLM might format code differently across runs (variable names, whitespace, comments). To improve hit rate, normalize the code before hashing: strip comments, normalize whitespace, sort variable declarations. This is best-effort — misses fall through to sandbox execution.

**Layer 3 — Post-execution structured key derivation** (after executor returns):

After the executor returns `ExecutionResult`, correlate the original `QueryPlan` with `step_results` to store results under canonical structured keys:

```python
def cache_step_results(user_id, plan, execution_result, computed_values):
    if plan.plan:
        for step_name, step in plan.plan.items():
            value = execution_result.step_results.get(step_name)
            if value is None:
                continue
            structured_key = build_step_key(step, plan.plan, computed_values)
            if structured_key:
                result_cache_set(user_id, structured_key, value)
    elif plan.items:
        for item in plan.items:
            parts = [p.strip() for p in item.split(",")]
            if len(parts) == 2:
                key = build_retrieve_key(parts[0], parts[1], "")
                result_cache_set(user_id, key, execution_result.final_answer)
```

`build_step_key` resolves step references in `step.args` (e.g., `"step1"`) back to the field/year they retrieved, then builds the canonical key:

```python
def build_step_key(step, all_steps, computed_values):
    if step.action == "retrieve":
        args = step.args
        if len(args) == 3:  # [Sheet, Field, Year]
            return f"{normalize_sheet(args[0])}.{normalize_field(args[1])}_{normalize_year(args[2])}"
        elif len(args) == 2:  # [Field, Year]
            return f"{normalize_field(args[0])}_{normalize_year(args[1])}"

    elif step.action in NAMED_OPERATIONS:
        canonical_op = OP_NAME_MAP.get(step.action, step.action)
        # Resolve step references to their structured keys
        resolved = []
        for arg in step.args:
            if arg.startswith("step"):
                ref_step = all_steps.get(arg)
                if ref_step and ref_step.action == "retrieve":
                    ref_key = build_step_key(ref_step, all_steps, computed_values)
                    resolved.append(ref_key)  # e.g., "revenue_2022"
                else:
                    return None  # can't derive key for non-retrieve refs
            else:
                resolved.append(f"lit_{arg}")  # literal number
        # Build: op_field1_year1_field2_year2
        return f"{canonical_op}_{'_'.join(resolved)}"

    elif step.action == "compute":
        return None  # compute steps use semantic alias layer only

    return None
```

#### Semantic Alias Layer (for `compute` steps)

`compute` steps are natural-language descriptions turned into Python code by the LLM. We can't derive structured keys deterministically. Instead:

1. **Embed the description** using `embed_query()` (same MiniLM model)
2. **Search for similar cached descriptions** for the same user (cosine similarity ≥ 0.90)
3. **If hit**: return the cached value, skip sandbox execution
4. **If miss**: execute in sandbox, store with description embedding + any structured key we can derive post-hoc

Redis key: `step_semantic:{user_id}:{sha256(description)}`
Redis value: hash with `description`, `value`, `embedding`, `structured_key` (nullable)

SQLite: same `result_cache` table, using `description` and `embedding_json` columns.

#### Worked Example

```
Query 1: "what is my total revenue for 2022-2025"

Planner produces:
  step1: retrieve ["Revenue", "2022"]
  step2: retrieve ["Revenue", "2023"]
  step3: retrieve ["Revenue", "2024"]
  step4: retrieve ["Revenue", "2025"]
  step5: add ["step1", "step2", "step3", "step4"]

Executor runs:
  retrieve(Revenue, 2022) → 32500     → Layer 1 miss, store result:{uid}:revenue_2022
  retrieve(Revenue, 2023) → 34000     → Layer 1 miss, store result:{uid}:revenue_2023
  retrieve(Revenue, 2024) → 35500     → Layer 1 miss, store result:{uid}:revenue_2024
  retrieve(Revenue, 2025) → 37000     → Layer 1 miss, store result:{uid}:revenue_2025
  execute_python_code("32500+34000+35500+37000") → 139000
    → Layer 2 miss, store sandbox:{uid}:{hash("32500+34000+35500+37000")}

Post-execution (Layer 3):
  step5 key = sum_revenue_2022_revenue_2023_revenue_2024_revenue_2025
  → store result:{uid}:sum_revenue_2022_2023_2024_2025 = 139000

Full response also cached in semantic cache (existing).
```

```
Query 2: "ratio of revenue to grants 2022-2025"

Planner produces:
  step1: retrieve ["Revenue", "2022"]
  step2: retrieve ["Revenue", "2023"]
  step3: retrieve ["Revenue", "2024"]
  step4: retrieve ["Revenue", "2025"]
  step5: retrieve ["Grants", "2022"]
  step6: retrieve ["Grants", "2023"]
  step7: retrieve ["Grants", "2024"]
  step8: retrieve ["Grants", "2025"]
  step9: add ["step1", "step2", "step3", "step4"]
  step10: add ["step5", "step6", "step7", "step8"]
  step11: divide ["step9", "step10"]

Executor runs:
  retrieve(Revenue, 2022) → Layer 1 HIT (32500, no DataFrame scan)
  retrieve(Revenue, 2023) → Layer 1 HIT (34000)
  retrieve(Revenue, 2024) → Layer 1 HIT (35500)
  retrieve(Revenue, 2025) → Layer 1 HIT (37000)
  retrieve(Grants, 2022)   → Layer 1 miss, store
  retrieve(Grants, 2023)   → Layer 1 miss, store
  retrieve(Grants, 2024)   → Layer 1 miss, store
  retrieve(Grants, 2025)   → Layer 1 miss, store
  execute_python_code("32500+34000+35500+37000") → Layer 2 HIT (139000, same code hash)
  execute_python_code("...grants sum...")         → Layer 2 miss, store
  execute_python_code("139000/grants_sum")        → Layer 2 miss, store

Post-execution (Layer 3):
  step9  → sum_revenue_2022_2023_2024_2025 = 139000 (already cached)
  step10 → sum_grants_2022_2023_2024_2025 = ...
  step11 → division_revenue_2022_2023_2024_2025_grants_2022_2023_2024_2025 = ...

8 of 11 tool calls served from cache (4 retrieve + 1 sandbox + 3 post-exec).
```

#### Edge Cases & Design Decisions

**1. Sheet-scoped vs unscoped retrieval**

`retrieve(["SheetA", "Revenue", "2022"])` returns a different value than `retrieve(["Revenue", "2022"])` — the latter searches all sheets and may return multiple values. Keys must distinguish:

- Unscoped: `revenue_2022` → value may be `"SheetA: 32500; SheetB: 31000"`
- Scoped: `sheetA.revenue_2022` → value is `"32500"`

These are different cache entries. A scoped retrieval does NOT satisfy an unscoped lookup (different result format).

**2. Multi-sheet retrieval returns a string, not a number**

Unscoped `retrieve` returns `"SheetA: 32500; SheetB: 31000"` — a semicolon-delimited string. The cache stores this string as-is. The executor agent parses it in subsequent tool calls. Cache value is always the raw tool return string.

**3. Step reference resolution for structured keys**

`add(["step1", "step2"])` references prior steps. To build `sum_revenue_2022_revenue_2023`, we must resolve `step1` → `revenue_2022` and `step2` → `revenue_2023` by looking up the referenced step's action and args in the `QueryPlan`. If a referenced step is itself a named operation (not a retrieve), we **cannot** derive a structured key — skip caching for that step (Layer 3 returns None). Layers 1 and 2 still cache the individual tool calls.

**4. Literal numbers in operation args**

`add(["step1", "100"])` — the `100` is a literal. Structured key: `sum_revenue_2022_lit_100`. The `lit_` prefix distinguishes literals from field/year references.

**5. `compute` steps are opaque**

A compute step like `"Calculate CAGR of Revenue 2021-2023 × 2023 profit margin"` is a single natural-language description. We cannot decompose it. The semantic alias layer (Layer 3) is the only caching mechanism. If the same description appears in a future plan, it hits. Paraphrased descriptions hit via embedding similarity.

**6. Executor might not follow the plan**

The executor is an LLM agent — it may call tools in a different order, make extra calls, or skip steps. Post-execution key derivation (Layer 3) correlates `step_results` with `QueryPlan` steps by name. If the executor's `step_results` keys don't match the plan's step names, Layer 3 silently skips those entries. Layers 1 and 2 still cache every tool call regardless.

**7. `retrieve_numbers` task type bypasses the plan**

When `task_type == "retrieve_numbers"`, the planner returns `items: ["Revenue, 2022"]` instead of a step-by-step plan. The executor still calls `retrieve` for each item, so Layer 1 caching works. Layer 3 handles this case by iterating `plan.items` instead of `plan.plan`.

**8. Year format variability**

DataFrame columns may be `"2022"`, `"FY2022"`, `"2022-2023"`, etc. The key uses the year string as-is from the plan (which comes from the sheet metadata). Normalization: strip whitespace, preserve original format. Two queries against the same sheet will produce the same year string.

**9. Field name normalization**

`"Wages and salaries"` → `wages_and_salaries`. Rules: lowercase, replace spaces and hyphens with underscores, strip non-alphanumeric chars (except underscores), collapse consecutive underscores.

**10. Operation disambiguation**

`divide(a, b)` = a/b. `ratio(a, b)` = a/b. `return_percentage(a, b)` = a/b*100. All three involve division but have different canonical names. The planner chooses which to use based on semantics; the cache preserves that choice. If a user asks "what percentage is X of Y" and later "what's the ratio of X to Y", the planner may use `return_percentage` then `ratio` — these are separate cache entries even though the underlying computation is similar.

**11. Cross-sheet calculations in schema groups**

When the planner uses `retrieve(["Revenue", "2022"])` across a schema group, it gets multiple values. A subsequent `compute` step processes them. The retrieve is cached as `revenue_2022` with the multi-value string. The compute step is cached via sandbox hash or semantic alias. If the user uploads a new sheet to the same schema group, the unscoped retrieval may return different values — cache invalidation on upload handles this.

**12. Concurrent queries from the same user**

Two simultaneous queries might both miss the cache and compute the same result. This is harmless — both store the same value. The second write overwrites the first (same key, same value). No corruption risk. If we later need strict deduplication, we can add a Redis `SETNX` lock, but the cost of duplicate computation is low.

**13. Cache size growth**

Each query generates 5-15 tool calls (retrieve + sandbox). At 100 queries/day per user, that's ~1K entries/day. With 7-day TTL, max ~7K entries per user. Redis handles this easily. SQLite fallback with 10K entries per user: cosine similarity scan takes ~50ms — acceptable. Beyond 10K, switch to Redis or add an index.

**14. `give_advice` task type**

Advice queries (`task_type == "give_advice"`) don't produce structured step results. The executor returns a text response. These are cached only by the existing semantic cache (full query → response). No intermediate result caching applies.

**15. Error values in cache**

`retrieve` can return error strings like `"ERROR: 'revenue' not found in any sheet."`. These should NOT be cached — a future upload might add the missing field. The `retrieve` tool checks for the `ERROR:` prefix and skips cache storage on error returns.

#### Storage Structure

**Redis** (primary, when `REDIS_URL` is set):

```
# Layer 1: Retrieve cache
result:{user_id}:revenue_2022                    → "32500"
result:{user_id}:sheetA.revenue_2022             → "32500"
result:{user_id}:revenue_2022_2023_2024_2025     → "[32500, 34000, 35500, 37000]"

# Layer 2: Sandbox cache
sandbox:{user_id}:{sha256(code)}                 → "139000"

# Layer 3: Post-execution structured keys
result:{user_id}:sum_revenue_2022_2023_2024_2025 → "139000"
result:{user_id}:ratio_revenue_grant_2022_2025   → "2.34"

# Layer 3: Semantic alias for compute steps
step_semantic:{user_id}:{sha256(description)}    → {
  "description": "grand total of all revenue",
  "value": 139000,
  "structured_key": "sum_revenue_2022_2023_2024_2025",
  "embedding": <1024-dim float32 bytes>
}

# Full query response (existing semantic cache, unchanged)
semantic:{user_id}:{hash}                        → {
  "query": "what is my total revenue for 2022-2025",
  "response": "{...}",
  "embedding": <1024-dim float32 bytes>
}
```

**SQLite fallback** (new table):

```sql
CREATE TABLE IF NOT EXISTS result_cache (
    cache_key TEXT NOT NULL,          -- structured key, sha256(code), or sha256(description)
    user_id TEXT NOT NULL,
    value TEXT NOT NULL,              -- JSON-serialized result (string, number, or list)
    cache_type TEXT NOT NULL,         -- 'retrieve', 'sandbox', or 'semantic_step'
    structured_key TEXT,              -- nullable; canonical key for post-execution entries
    description TEXT,                 -- nullable; plain-English for compute steps
    embedding_json TEXT,              -- nullable; 1024-dim for semantic alias
    created_at TEXT DEFAULT (datetime('now')),
    last_accessed TEXT DEFAULT (datetime('now')),
    hit_count INTEGER DEFAULT 0,
    PRIMARY KEY (cache_key, user_id)
);

CREATE INDEX IF NOT EXISTS idx_result_cache_user
ON result_cache(user_id);

CREATE INDEX IF NOT EXISTS idx_result_cache_user_type
ON result_cache(user_id, cache_type);
```

#### Cache Invalidation

Same triggers as the semantic cache:
- **File upload** → `invalidate_user_cache(user_id)` clears all `result:{user_id}:*`, `sandbox:{user_id}:*`, `step_semantic:{user_id}:*`, and `semantic:{user_id}:*` keys
- **File delete** → same invalidation
- **TTL** → 7 days auto-expiry (Redis native, SQLite cron job)

No partial invalidation — if the underlying data changes, all cached results for that user are stale. Granular invalidation (e.g., only revenue entries when a revenue sheet changes) is not feasible because we don't track which sheets contribute to which cached results.

#### Implementation Steps

1. **Add `result_cache.py`**:
   - `build_retrieve_key(field, year, sheet) -> str`
   - `build_step_key(step, all_steps) -> str | None`
   - `normalize_field(name) -> str`, `normalize_year(year) -> str`, `normalize_sheet(name) -> str`
   - `normalize_operation(name) -> str` (code name → canonical)
   - `result_cache_get(user_id, key) -> str | None`
   - `result_cache_set(user_id, key, value, cache_type, description, structured_key, embedding)`
   - `sandbox_cache_get(user_id, code_hash) -> str | None`
   - `sandbox_cache_set(user_id, code_hash, value)`
   - `find_similar_step(user_id, embedding, threshold) -> dict | None`
   - `invalidate_user_results(user_id) -> int`
   - Redis + SQLite fallback (same pattern as `cache_service.py`)

2. **Add `result_cache` table** to `sheet_metadata.py` `init_db()` with migration

3. **Modify `retrieve` tool** in `pipeline.py` — add Layer 1 cache check before DataFrame scan, store on miss (skip if error return)

4. **Modify `execute_python_code` tool** in `pipeline.py` — add Layer 2 cache check before sandbox execution, store on miss. Normalize code before hashing (strip comments, normalize whitespace).

5. **Add post-execution caching** in `run_pipeline()` — after executor returns, call `cache_step_results(user_id, plan, execution_result)` to derive and store structured keys (Layer 3)

6. **Wire `user_id` into pipeline** — add `user_id: str` field to `PipelineDeps`, pass from `main.py:query_rag()` through `build_query_pipeline()`

7. **Update `invalidate_user_cache`** in `semantic_cache.py` — also call `invalidate_user_results()` from `result_cache.py` to clear all three layers

8. **Tests** — `test_result_cache.py`:
   - Structured key generation: retrieve (scoped + unscoped), all operation types, literal args
   - Alias normalization (sum = total = grand_total, division = ratio = divided)
   - Layer 1: retrieve cache hit skips DataFrame scan
   - Layer 2: sandbox cache hit skips execution
   - Layer 3: post-execution keys derived correctly from plan + step_results
   - Per-user isolation
   - Error returns not cached
   - Invalidation on file upload clears all three layers
   - Semantic alias match for compute steps
   - `retrieve_numbers` task type (items path)
   - Concurrent writes (same key, same value — no corruption)

#### What This Does NOT Cache

- **Sheet descriptions** — handled by the existing exact-match cache in `cache_service.py`
- **Full query responses** — handled by the existing semantic cache in `semantic_cache.py`
- **DataFrame transformations** — the pipeline loads sheets into DataFrames every time; caching DataFrames is out of scope (they're derived from S3 files, not computed)
- **Planner agent output** — the `QueryPlan` is regenerated every time (cheap, ~80 tokens)
- **Responder agent output** — the friendly response is regenerated every time (cheap, ~80 tokens). The semantic cache may catch full-query duplicates, but intermediate result caching doesn't cover this.

#### Cost Impact

With intermediate result caching:
- **Retrieval steps**: O(1) Redis lookup instead of DataFrame scan (~0.1ms vs ~5ms)
- **Sandbox steps**: O(1) Redis lookup instead of Python sandbox execution (~0.1ms vs ~50ms)
- **LLM calls**: Unchanged — planner and responder agents still run (they're cheap, ~80 tokens each)
- **Net**: For multi-step queries that share sub-results, 40-60% of tool calls served from cache
- **At scale**: 100 queries/day/user, 10 steps each, 50% cache hit rate → 500 sandbox executions saved/day/user

---

## Redis: Architecture & Dual-Write Strategy

### Why Redis Alongside SQLite

SQLite is the **source of truth** — it survives any crash, requires no external dependency, and handles all cache lookups when Redis is unavailable. Redis is an **optional acceleration layer** that provides:

| Capability | SQLite Alone | With Redis |
|-----------|-------------|------------|
| **Lookup latency** | ~1-5ms (disk I/O) | ~0.1ms (in-memory) |
| **Vector search** | Brute-force NumPy cosine scan — O(n), fine up to ~10K entries/user | `FT.SEARCH` KNN — O(log n), scales to millions |
| **TTL / auto-expiry** | Manual cron job (`cleanup_old_cache_entries`) | Native `SETEX` — auto-expires keys |
| **Concurrent writes** | Single-writer, file-locked | Multi-writer, atomic operations |
| **Durability** | Survives any crash | Can lose data (even with AOF, not as durable) |
| **External dependency** | None | Requires `REDIS_URL` + Redis server |

### Dual-Write Pattern

When `REDIS_URL` is set, both stores are written on every cache entry. Reads hit Redis first; on miss, fall through to SQLite:

```
Write: cache_set(model, prompt, response)
  ├── Redis: r.setex(key, ttl, response)     ← fast write
  └── SQLite: set_cached_response(model, prompt, response)  ← durable write

Read: cache_get(model, prompt)
  ├── Redis: r.get(key)                       ← ~0.1ms
  │     HIT → return
  │     MISS ↓
  └── SQLite: get_cached_response(model, prompt)  ← ~1-5ms
        HIT → return (and optionally backfill Redis)
        MISS → return None (caller proceeds to LLM)
```

**Why dual-write is not redundant:**

1. **Graceful degradation**: If Redis crashes or is unreachable, all reads fall through to SQLite — no data loss, no downtime. The `cache_service.py` wrapper handles this transparently via `_get_redis()` returning `None`.

2. **Restart recovery**: On Redis restart (which clears its in-memory data), SQLite has every entry. Redis can be repopulated lazily (on first read miss, backfill from SQLite) or via a bulk migration script.

3. **Different failure modes**: SQLite can corrupt on disk failure; Redis can lose data on OOM kill. Having both means a single failure never loses all cache data.

4. **No overhead when disabled**: If `REDIS_URL` is not set, `_get_redis()` returns `None` and all operations go directly to SQLite. Zero Redis code runs, zero overhead.

### Redis Key Namespaces

| Namespace | Purpose | TTL | Example |
|-----------|---------|-----|---------|
| `llm:{sha256}` | Exact-match LLM cache (Layer 1) | 7 days | `llm:a1b2c3...` |
| `semantic:{user_id}:{hash}` | Semantic cache (Layer 2) | 7 days | `semantic:user42:d4e5f6...` |
| `result:{user_id}:{key}` | Retrieve cache (Layer 3) | 7 days | `result:user42:revenue_2022` |
| `sandbox:{user_id}:{hash}` | Sandbox cache (Layer 4) | 7 days | `sandbox:user42:g7h8i9...` |
| `step_semantic:{user_id}:{hash}` | Compute step semantic alias (Layer 5) | 7 days | `step_semantic:user42:j0k1l2...` |

### Redis Vector Search (RediSearch)

For semantic cache lookups at scale, Redis Stack's RediSearch module provides KNN vector similarity search — replacing the brute-force NumPy cosine scan used in SQLite-only mode:

```bash
# Create vector index (384-dim for MiniLM, 1024-dim for Qwen3)
FT.CREATE idx_semantic ON JSON PREFIX 1 semantic: SCHEMA
  $.user_id TAG SEPARATOR ","
  $.embedding VECTOR FLAT 6 TYPE FLOAT32 DIM 384 DISTANCE_METRIC COSINE
```

```python
# KNN query — O(log n) instead of O(n)
FT.SEARCH idx_semantic "*=>[KNN 1 @embedding $vec]" 
  PARAMS 2 vec $blob 
  SORTBY __score
  LIMIT 0 1
```

**When to switch from SQLite to Redis vector search:**
- <10K cache entries per user: SQLite NumPy scan is fine (~50ms)
- >10K entries: switch to Redis `FT.SEARCH` (~5ms)
- >100K entries: Redis is required — SQLite scan becomes a bottleneck

### Connection Management

```python
# cache_service.py — lazy singleton with fallback
_redis_client = None

def _get_redis():
    global _redis_client
    if _redis_client is None:
        url = os.environ.get("REDIS_URL")
        if url:
            _redis_client = redis.from_url(url, decode_responses=True)
    return _redis_client
```

- **Lazy initialization**: Redis connection is created on first use, not at import time
- **Singleton**: One connection reused across all cache operations
- **No health check**: If Redis goes down, `redis.from_url()` returns a client that raises `ConnectionError` on use — caught by the try/except in `cache_get`/`cache_set`, which falls through to SQLite
- **Connection pooling**: `redis.from_url()` uses a connection pool by default (max 10 connections)

### Migration Path

| Phase | Redis | SQLite | Status |
|-------|-------|--------|--------|
| **Phase 1** (current) | Optional, dual-write | Source of truth, always active | ✅ Implemented |
| **Phase 2** (future) | Primary read path, SQLite as backup | Write-only (no reads unless Redis down) | Planned |
| **Phase 3** (scale) | Primary, with vector search | Cold backup / migration only | Future |

### Cost

| Environment | Provider | Cost |
|-----------|----------|------|
| **Dev** | Local Docker (`redis:7-alpine`) | $0 |
| **Prod** | Upstash free tier | $0 (256MB, 500K commands/mo, 10GB bandwidth, no credit card) |
| **Prod (scaled)** | Upstash pay-as-you-go | $0.20 per 100K commands + $0.25/GB-mo storage |
| **Prod (high traffic)** | Upstash fixed plan | $10–$800/mo (flat, unlimited commands) |

**Not using**: AWS ElastiCache (no indefinite free tier — 12-month trial only, ~$12/mo for `cache.t4g.micro` after trial, always-on node-hour billing).

**Why not other providers**:
- **Aiven** (1GB free, Valkey): No RediSearch module — can't do vector similarity search for semantic cache
- **Redis Cloud** (30MB free): Too small for production; RediSearch available but limited to paid plans at scale
- **Google Memorystore / Azure Cache**: No free tier, per-node-hour billing (~$36/mo / ~$16/mo entry)
- **DigitalOcean** ($15/mo): No free tier, runs Valkey (no modules)

