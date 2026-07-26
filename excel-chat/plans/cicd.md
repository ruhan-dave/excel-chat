# Railway Deployment Plan

## Architecture Overview

```
Railway Project
├── Frontend Service (nginx + React static files)
│   ├── Built from frontend/Dockerfile (multi-stage: Node build → nginx serve)
│   ├── Serves static files on port 80
│   └── Proxies /api/* to backend via Railway private networking
│
├── Backend Service (FastAPI + uvicorn)
│   ├── Built from backend/Dockerfile (python:3.12-alpine)
│   ├── Runs on PORT env var (Railway sets automatically)
│   ├── Connects to: S3 (file storage), OpenRouter (LLM), Redis (optional cache)
│   └── Persistent volume at /app/data for SQLite metadata
│
├── Redis Service (Railway plugin — optional)
│   └── Used for LLM response caching + semantic cache
│
└── Persistent Volume
    └── Mounted to backend at /app/data
        └── Stores sheets.db (SQLite metadata + cache fallback)
```

### Why This Architecture

**Two-service split (frontend + backend)** rather than a single monolithic container:
- **Independent scaling**: Frontend (static files) can serve thousands of requests with minimal CPU. Backend (LLM calls, S3 I/O) is the bottleneck — it can be scaled independently.
- **Independent deploys**: Frontend UI changes can be deployed without restarting the backend (which would interrupt active LLM queries).
- **Security**: The backend's API is not directly exposed to the internet. Nginx proxies only `/api/*` requests, and the backend service doesn't need a public domain.
- **Cost efficiency**: Frontend container can be very small (nginx + static files ~25MB). Backend container is larger (Python + ML libraries) but only needs resources when processing queries.

**Why nginx for the frontend** (instead of a Node.js server or CDN):
- The app uses client-side routing (React SPA with tabs). Nginx's `try_files $uri /index.html` handles SPA fallback correctly.
- Nginx proxies `/api/*` to the backend, keeping the browser's requests same-origin (no CORS issues in the common case).
- Nginx is ~2MB, battle-tested, and requires zero configuration at runtime. A Node.js server would add unnecessary memory overhead (~50MB) for serving static files.
- Railway doesn't have a built-in CDN. Nginx with `expires -1` on `/api` and cache headers on static assets is a reasonable approximation.

**Why not a CDN (Cloudflare, Vercel) for the frontend?**
- Could be a future optimization. For now, keeping frontend and backend in the same Railway project simplifies deployment and enables private networking. Moving the frontend to a CDN later only requires changing the nginx proxy to use the backend's public URL (Option B in §1.2).

---

## Storage Architecture

### Overview

The app uses a **three-tier storage model**: S3 for raw files, SQLite on a Railway volume for metadata + cache fallback, and Redis (Upstash) for hot cache. Each tier serves a different purpose and has different persistence guarantees.

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Railway Container                            │
│                                                                     │
│  ┌─────────────────────────────────────────┐  ┌──────────────────┐ │
│  │ Ephemeral Filesystem (wiped on redeploy) │  │ Volume Mount     │ │
│  │                                          │  │ /app/data/       │ │
│  │  /app/src/        ← Python source code   │  │  └── sheets.db   │ │
│  │  /app/venv/       ← Python dependencies  │  │      (SQLite)    │ │
│  │  /tmp/uploads/    ← Temp S3 downloads    │  │                  │ │
│  │  ~/.cache/        ← HuggingFace model    │  │  Persistent      │ │
│  │                     cache (SentenceTrfm) │  │  across redeploys│ │
│  └─────────────────────────────────────────┘  └──────────────────┘ │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ In-Memory (lost on restart)                                   │   │
│  │  _redis_client  ← TCP/TLS socket to Upstash Redis             │   │
│  │  _model         ← SentenceTransformer (all-MiniLM-L6-v2)      │   │
│  │  file_cache     ← pandas DataFrames for current query          │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
         │                              │
         │ TCP/TLS (port 6379)          │ HTTPS (S3 API)
         ▼                              ▼
┌─────────────────────┐    ┌──────────────────────────────┐
│ Upstash Redis       │    │ AWS S3 (ragsheets bucket)     │
│ (external service)  │    │                               │
│                     │    │ uploads/{uuid}/{filename}.xlsx│
│ Key namespaces:     │    │                               │
│  llm:{hash}         │    │ Lifecycle: 90-day expiry      │
│  semantic:{user}:.. │    │                               │
│  result:{user}:{key}│    │ Stores raw .xlsx files only   │
│  sandbox:{user}:..  │    │ (1-50MB each)                 │
│                     │    │                               │
│ TTL: 7 days         │    │ Persistent (cloud storage)    │
│ Pay-per-request     │    │                               │
└─────────────────────┘    └──────────────────────────────┘
```

### Tier 1: AWS S3 — Raw Excel File Storage

| Property | Value |
|----------|-------|
| **Bucket** | `ragsheets` (us-east-1) |
| **Path pattern** | `uploads/{file_id}/{original_filename}.xlsx` |
| **What's stored** | The original `.xlsx` files users upload (1-50MB each) |
| **Persistence** | Cloud storage — survives all container restarts, redeploys, crashes |
| **Lifecycle** | S3 lifecycle policy expires objects after 90 days |
| **Access pattern** | Upload on file upload → download on query (streamed into pandas) |
| **Cost** | ~$0.023/GB/month (S3 Standard) |

S3 is the **source of truth for raw data**. When a user asks "What is revenue in 2022?", the backend downloads the relevant `.xlsx` from S3, loads it into a pandas DataFrame, runs the query, then discards the DataFrame. The file is never stored locally — it's streamed from S3 on demand.

### Tier 2: SQLite — Metadata + Cache Fallback

| Property | Value |
|----------|-------|
| **File** | `sheets.db` (single SQLite file) |
| **Location** | `/app/data/sheets.db` (Railway volume mount) |
| **Path config** | `DB_PATH` env var → defaults to `{DATA_DIR}/sheets.db` |
| **What's stored** | File records, sheet metadata, LLM cache, result cache, threads, messages |
| **Persistence** | Persistent via Railway volume — survives redeploys |
| **Access pattern** | Direct file I/O via Python's `sqlite3` module (no network) |
| **Size** | ~1KB per sheet record, ~2-5KB per cache entry (1GB volume = ~1M records) |

SQLite stores **metadata about what's in S3**, not the Excel data itself. This is the "card catalog" — the app queries SQLite to find which sheets have "Revenue" as a field and "2022" as a year, then downloads only that sheet from S3.

**Tables in `sheets.db`:**

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `files` | One row per uploaded file | `file_id`, `file_name`, `s3_key`, `sheet_count`, `user_id` |
| `sheets` | One row per sheet within a file | `sheet_id`, `file_id`, `sheet_name`, `fields_json`, `years_json`, `user_description` |
| `llm_cache` | Cached LLM responses + embeddings | `cache_key`, `response`, `model`, `user_id`, `embedding_json` |
| `result_cache` | Cached intermediate pipeline results | `cache_key`, `user_id`, `value`, `cache_type`, `structured_key` |
| `threads` | Conversation threads | `thread_id`, `user_id`, `title`, `created_at` |
| `thread_sheets` | Sheets attached to a thread | `thread_id`, `sheet_id` |
| `messages` | Q&A messages within threads | `message_id`, `thread_id`, `role`, `content`, `full_result` |

**Why SQLite (not PostgreSQL)?**
- Single file, zero operational overhead — no server, no connection pool, no network latency
- Fast for low-traffic apps (capstone project)
- Easy migration path: code uses raw SQL, swap `sqlite3` for `psycopg2` when needed
- When to migrate: >5 concurrent users, multi-instance scaling, or automatic backups needed

### Tier 3: Redis (Upstash) — Hot Cache

| Property | Value |
|----------|-------|
| **Provider** | Upstash (serverless Redis, pay-per-request) |
| **Connection** | `REDIS_URL` env var → TCP/TLS socket on port 6379 |
| **What's stored** | LLM response cache, semantic vector cache, result cache, sandbox cache |
| **Persistence** | External service — independent of Railway container lifecycle |
| **TTL** | 7 days (auto-expiry, refreshed on access) |
| **Access pattern** | Network call via `redis` Python library (shared singleton connection) |

Redis is the **fast read path**. Every cache lookup checks Redis first (~0.1ms network to Upstash), then falls back to SQLite if Redis misses. Every cache write goes to **both** Redis and SQLite (dual-write) — Redis for speed, SQLite for durability.

**5 Redis key namespaces:**

| Key pattern | Cache layer | What it stores |
|-------------|-------------|----------------|
| `llm:{sha256}` | Exact-match LLM cache | Full LLM response for a (model, prompt) pair |
| `semantic:{user_id}:{hash}` | Semantic cache | Query embedding + response (RediSearch vector index) |
| `result:{user_id}:{key}` | Retrieve cache | Raw tool return values (field/year/sheet lookups) |
| `sandbox:{user_id}:{hash}` | Sandbox cache | Python code execution results |
| `{key}:hits` | Hit counter | Incremented on each cache hit (observability) |

### Container Filesystem Layers

The Railway container has three distinct storage zones with different lifecycles:

```
/app/                          ← Ephemeral (rebuilt from Docker image on each deploy)
├── src/                       ← Python source code (from git)
├── venv/                      ← Python dependencies (from pip install)
├── uploads/                   ← Temp staging for file uploads (deleted after S3 upload)
└── ...

/tmp/                          ← Ephemeral (container RAM/disk, wiped on restart)
└── uploads/                   ← S3 downloads during query processing (transient)

/app/data/                     ← PERSISTENT VOLUME MOUNT
└── sheets.db                  ← SQLite database (survives redeploys)

~/.cache/huggingface/          ← Ephemeral (re-downloaded on each deploy)
└── models--all-MiniLM-L6-v2/  ← SentenceTransformer model (~80MB)
```

**What happens on redeploy:**
- `/app/src/`, `/app/venv/` → Rebuilt from Docker image (new code)
- `/tmp/` → Wiped clean (fresh container)
- `~/.cache/` → Wiped (model re-downloads on first query — ~5 seconds)
- `/app/data/sheets.db` → **Preserved** (volume mount persists across container replacements)

### Dual-Write Cache Strategy

Every cache write goes to both Redis and SQLite. Every cache read checks Redis first, then SQLite:

```
Write path (e.g., cache_set):
  1. Redis SETEX (key, ttl=7d, value)     ← fast, network to Upstash
  2. SQLite INSERT OR REPLACE             ← durable, local file I/O
  (both attempted; failures logged, never raised)

Read path (e.g., cache_get):
  1. Redis GET (key)                      ← ~0.1ms
     ├─ HIT  → return value (refresh TTL, increment hit counter)
     └─ MISS → fall through to SQLite
  2. SQLite SELECT                        ← ~1ms local disk
     ├─ HIT  → return value
     └─ MISS → return None (cache miss, proceed to LLM call)
```

**Why dual-write?**
- Redis is fast but ephemeral (Upstash could have outages, key eviction)
- SQLite is durable but slower (disk I/O, single-writer lock)
- Dual-write means either store can fail and the app still works
- Redis is the **performance** layer; SQLite is the **durability** layer

### Data Flow: Upload → Query → Cache

```
1. User uploads Excel file
   → POST /api/upload/
   → File saved to S3 (uploads/{uuid}/{filename}.xlsx)
   → ExcelService.load_sheet_metadata_from_file() parses fields, years
   → save_file() + save_sheet() → SQLite (metadata only)
   → invalidate_user_cache() → Redis + SQLite cache cleared (stale data)

2. User asks "What is revenue in 2022?"
   → GET /api/query/stream?query=...&thread_id=...&sheet_ids=...
   → embed_query(query) → 384-dim vector via SentenceTransformer
   → find_similar_cached(user_id, embedding) → Redis FT.SEARCH (KNN)
     ├─ HIT (similarity ≥ 0.88) → return cached response, persist to messages
     └─ MISS → continue to pipeline
   → Pipeline: plan → retrieve (S3 download) → compute → sandbox
     ├─ result_cache_get() → Redis GET, SQLite fallback (per intermediate step)
     └─ sandbox_cache_get() → Redis GET, SQLite fallback (per code block)
   → store_cached() → Redis HSET + SQLite INSERT (semantic cache, dual-write)
   → cache_step_results() → Redis SETEX + SQLite INSERT (structured keys)
   → save_message() → SQLite only (thread persistence)
   → SSE stream → frontend
```

---

## Phase 1: Code Changes (must do before deploying)

### 1.1 Fix frontend API URL — use relative path

**File**: `frontend/ragsheets/.env.production`

Vite inlines env vars at build time. Since nginx already proxies `/api` to the backend, the frontend should use a relative URL so it works regardless of domain.

```env
VITE_API_ENDPOINT="/api"
```

**Why**: The current value `http://vcm-47087.vm.duke.edu/api` is a Duke VM that won't be accessible from Railway. With a relative path, the browser requests `/api/...` which nginx proxies to the backend.

**Why a relative path instead of an absolute URL:**
- **Domain portability**: The app works on any domain (`ragsheets.up.railway.app`, a custom domain, or `localhost`) without rebuilding. An absolute URL would need to be updated and the frontend rebuilt whenever the domain changes.
- **Same-origin requests**: The browser sees `/api/sheets/` as same-origin, so no CORS preflight requests are needed. This reduces latency by one round-trip per API call.
- **Vite build-time inlining**: Vite replaces `import.meta.env.VITE_API_ENDPOINT` at build time, not runtime. This means the value is baked into the JS bundle. A relative path is the only value that works across all environments without rebuilds.

**Why not use a runtime env var (e.g., `window.__ENV__`)?**
- That would require a Node.js server or a runtime template injection step. For a static SPA served by nginx, build-time inlining is the standard approach. The relative path `/api` is environment-agnostic, so this is a non-issue.

### 1.2 Fix nginx proxy target for Railway

**File**: `frontend/default.conf`

Railway services communicate via internal URLs, not Docker Compose service names. The current `proxy_pass http://backend:8000` uses Docker Compose's DNS, which doesn't exist on Railway.

**Two options:**

**Option A (recommended): Use Railway's private networking**

Railway assigns each service a private IPv4 address. Services in the same project can reach each other via `<service-name>.railway.internal`.

```nginx
location /api {
    proxy_pass http://backend.railway.internal:8000;
    proxy_set_header Host $host;
    proxy_set_header ORIGIN $http_origin;
    add_header Access-Control-Allow-Origin $http_origin;
    expires -1;
}
```

**Why Option A is the best decision:**
- **No Dockerfile changes**: The nginx config is static — no `envsubst` template processing needed at container startup.
- **Traffic stays private**: Requests between frontend and backend never traverse the public internet. Lower latency, no egress costs, and the backend doesn't need a public domain.
- **Simpler secrets management**: The backend URL is not a secret (it's internal), so no env var injection is needed.
- **Railway private networking is enabled by default** for all services in the same project. No extra configuration needed.

**Option B (fallback): Use the backend's public Railway URL**

If private networking doesn't work (e.g., services in different Railway projects), use the backend's public URL with `envsubst`:

```dockerfile
# frontend/Dockerfile — add at the end
CMD ["sh", "-c", "envsubst < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"]
```

Rename `default.conf` to `default.conf.template` and use `proxy_pass ${BACKEND_URL};`.

**Why Option B is less ideal:**
- Requires a Dockerfile change and a build-time env var (`BACKEND_URL`).
- Traffic goes over the public internet (higher latency, potential egress costs).
- The backend must have a public domain, increasing attack surface.
- If the backend's domain changes, you must update the env var and redeploy.

**Decision**: Option A (private networking). Implemented in the current code.

**Additional nginx considerations:**
- `proxy_set_header Host $host` — preserves the original Host header so the backend knows which domain the user visited.
- `proxy_set_header ORIGIN $http_origin` — forwards the browser's Origin header for CORS processing.
- `expires -1` on `/api` — disables caching for API responses. Static assets (JS, CSS) get nginx's default cache headers, which include ETag and Last-Modified for conditional requests.
- **Missing but recommended**: Add `proxy_read_timeout 120s` to `location /api` — the default nginx timeout is 60s, but LLM queries can take up to 120s (the backend's own timeout). Without this, nginx will return 504 Gateway Timeout before the backend finishes. See Phase 4 for details.

### 1.3 Add persistent volume for SQLite

**File**: `backend/src/sheet_metadata.py`

```python
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "sheets.db")
```

**Railway setup**: Add a persistent volume mounted at `/app/data` and set `DATA_DIR=/app/data`.

**Why**: Railway containers are ephemeral. Without a volume, `sheets.db` is wiped on every redeploy, losing all file metadata, sheet descriptions, and cache entries.

#### Storage Architecture — What Lives Where

| Data | Where it lives | Persistent? | Why |
|------|---------------|-------------|-----|
| **Excel files** (`.xlsx`) | **AWS S3** (`ragsheets` bucket) | ✅ Cloud, permanent (90-day lifecycle) | Files are large (1-50MB), accessed infrequently, and need to survive container restarts. S3 is the standard choice for object storage. |
| **Sheet metadata** (file names, sheet names, fields, descriptions, schema groups) | **SQLite** (`sheets.db`) | ❌ Ephemeral without volume | Metadata is small (~1KB per sheet), accessed on every request, and needs fast queries. SQLite is the right tool — no network latency, no connection pool overhead. |
| **LLM cache** (cached responses, embeddings) | **SQLite** (`sheets.db`) or Redis | ❌ Ephemeral without volume | Cache is a performance optimization. Losing it causes cache misses (slower responses) but no data loss. Redis is preferred when available (native TTL, better concurrency). |
| **Temp files** (downloaded from S3 during queries) | `/tmp/uploads` | ❌ Transient | These are Excel files downloaded from S3, loaded into pandas DataFrames for query processing, then discarded. No persistence needed — they can be re-downloaded from S3 at any time. |

The actual user data (Excel files) is already in S3 — that's cloud storage and survives redeploys fine. The problem is the **metadata**: `sheets.db` stores which files exist, their sheet names, field lists, user descriptions, auto-descriptions, and cache entries. Without the volume, every Railway redeploy wipes `sheets.db`, and the app loses track of all uploaded files even though they still exist in S3.

**Why `/app/data` specifically?** The backend Dockerfile sets `WORKDIR /app/src`, so the app runs from `/app/src`. Without the volume, `sheets.db` is written to `/app/src/sheets.db` (inside the container filesystem). We added `DATA_DIR=/app/data` so the DB file goes to `/app/data/sheets.db` — a separate mount point that Railway's persistent volume attaches to. This way, only the database persists, not the entire app directory.

**Why not mount the volume at `/app/src/`?**
- Mounting at the app directory would persist the entire directory, including source code. This is wasteful (source code is in the Docker image) and can cause issues: if you deploy new code, the volume's old source files could shadow the new ones.
- Mounting at a separate path (`/app/data`) cleanly separates persistent data from ephemeral code. This is a standard 12-factor app pattern: the app writes state to a configured path, and the infrastructure mounts storage there.

**Why SQLite and not PostgreSQL from the start?**
- **Simplicity**: SQLite is a single file — no database server, no connection pool, no network latency. For a capstone project with low traffic, this is the right tradeoff.
- **Zero operational overhead**: No database to manage, backup, or monitor. The volume handles persistence.
- **Easy migration path**: The codebase uses raw SQL queries (not an ORM), so migrating to PostgreSQL later means changing the connection string and a few SQLite-specific syntax items. See Phase 4 for migration criteria.
- **When to migrate**: More than 5 concurrent users, need for multi-instance backend scaling, or need for automatic backups. At that point, Railway's managed PostgreSQL is a one-click upgrade.

**In short**: User data (Excel files) is already safely in S3. The volume just protects the metadata database that tracks what's in S3.

### 1.4 Fix UPLOAD_FOLDER for temp files

**File**: `backend/src/main.py`

```python
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "./uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
```

**Why `/tmp/uploads` in production (not the persistent volume):**
- Temp files are Excel files downloaded from S3 during query processing. They're loaded into pandas DataFrames, used for the query, then never referenced again.
- They can be re-downloaded from S3 at any time — there's no data loss if they're deleted.
- Storing them on the persistent volume would waste storage (Excel files can be 1-50MB each) and require manual cleanup.
- `/tmp` is the standard location for transient files in Unix containers. Railway containers have a writable `/tmp` directory.
- **Local dev**: Defaults to `./uploads` (relative to the working directory), which is the original behavior. No local dev workflow changes needed.

**Why not clean up temp files immediately after query?**
- The current code downloads files from S3 and caches them in `file_cache` during a single query request. If the same file is needed for multiple sheets (multi-sheet query), it's only downloaded once. Cleanup happens when the request ends and the container eventually garbage-collects `/tmp`.
- For production, add a daily cleanup step (see Phase 4 — Temp file cleanup).

### 1.5 Update CORS origins default

**File**: `backend/src/main.py`

```python
_default_origins = "http://localhost:5173,http://localhost:3000"
origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
```

#### What is CORS_ORIGINS?

`CORS_ORIGINS` is an environment variable that controls which frontend domains are allowed to make API requests to the backend. It's used in `backend/src/main.py`:

```python
_default_origins = "http://localhost:5173,http://localhost:3000"
origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",") if o.strip()]
```

When the browser (frontend) makes a request to the backend, the backend checks if the frontend's origin is in this list. If not, the browser blocks the request. On Railway, you'd set it to your frontend's Railway domain, e.g.:

```
CORS_ORIGINS=https://ragsheets-frontend.up.railway.app
```

**Note**: Since we switched the frontend to use `VITE_API_ENDPOINT="/api"` and nginx proxies `/api` to the backend, the browser actually makes requests to the **same origin** (the frontend domain). The nginx proxy handles the cross-service communication internally. So CORS may not even be needed in this setup — the browser never talks directly to the backend. But keeping it configured is still good practice in case you later separate the domains or add a mobile client.

**Why we removed the Duke VM from defaults:**
- The Duke VM (`vmm-45508.vm.duke.edu`) is a development/staging environment that won't be used in production. Leaving it in defaults creates a false sense of security (it's an allowed origin that we don't control).
- Defaults should only include localhost dev servers (`5173` for Vite, `3000` for Node/Next.js). Production origins are always set via `CORS_ORIGINS` env var.

**Security best practice**: Set `CORS_ORIGINS` to only the exact domains you control. Never use `*` (allow all origins) in production — it disables CORS protection entirely. The current FastAPI middleware uses `allow_credentials=True`, which is incompatible with `*` anyway (browsers reject `Access-Control-Allow-Origin: *` when credentials are included).

### 1.6 Create per-service `railway.toml` files

Railway's config-as-code format only supports `[build]` and `[deploy]` as top-level sections — each `railway.toml` file defines config for **a single service**. There is no `[services.*]` syntax for multi-service repos. Instead, each service gets its own `railway.toml` in its directory.

**File**: `backend/railway.toml`

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

**File**: `frontend/railway.toml`

```toml
[build]
builder = "DOCKERFILE"
dockerfilePath = "Dockerfile"

[deploy]
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 3
```

**Railway dashboard setup**: When creating each service, set the config source location to the service's `railway.toml` path (e.g., `/backend/railway.toml` for the backend service, `/frontend/railway.toml` for the frontend service). This is configured under Service Settings → Config as Code.

**Why per-service files instead of a single root-level file:**
- Railway's config-as-code is designed for one service per file. A single `railway.toml` at the repo root would only apply to one service — the other service would need a custom path anyway.
- Per-service files are cleaner: each file only contains the config relevant to that service. No risk of accidentally applying backend settings to the frontend or vice versa.
- The `[build]` section specifies `builder = "DOCKERFILE"` to explicitly use the Dockerfile (Railway defaults to Railpack if not specified). The `dockerfilePath` is relative to the service's root directory.

**Why `railway.toml` instead of using only the Railway dashboard UI:**
- **Reproducibility**: Anyone in the team can deploy the same configuration by connecting the repo. No "click this in the dashboard" tribal knowledge.
- **Version control**: Infrastructure config is code — it's reviewed, tracked, and rollback-able via git.
- **Config overrides**: Railway merges config-as-code with dashboard settings. Code-defined values always override dashboard values, so the repo is the source of truth for build/deploy behavior.

**Why not Docker Compose on Railway:**
- Railway's `railway.toml` is its native config-as-code format. Docker Compose is for local development. While Railway has some Docker Compose support, it's not as well-documented or reliable as `railway.toml`.
- The existing `docker_compose.yml` remains for local development. Railway uses per-service `railway.toml` files for production. This separation is intentional — local dev and production have different needs (e.g., local dev uses `env_file: .env`, production uses Railway's env var injection).

---

## Phase 2: Railway Dashboard Configuration

### 2.1 Create Railway project

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Select the `excel-chat` repo
3. Create two services manually:
   - **Backend service**: New Service → GitHub Repo → Set root directory to `backend/` → Railway detects `backend/railway.toml` and `backend/Dockerfile`
   - **Frontend service**: New Service → GitHub Repo → Set root directory to `frontend/` → Railway detects `frontend/railway.toml` and `frontend/Dockerfile`
4. Under each service's Settings → Config as Code, verify the config source path points to the correct `railway.toml`

**Why deploy from GitHub (not Railway CLI):**
- **Automatic deploys**: Every push to the connected branch triggers a rebuild. This is CI/CD without setting up GitHub Actions.
- **Preview deployments**: Railway can create ephemeral preview environments for pull requests, allowing you to test changes before merging.
- **Build logs**: Railway shows build logs in the dashboard, making it easy to debug build failures.

### 2.2 Backend service — environment variables

Set these in the Railway dashboard under the backend service → Variables:

| Variable | Value | Required | Purpose |
|----------|-------|----------|---------|
| `OPENROUTER_API_KEY` | `sk-or-v1-...` (from `.env`) | **Yes** | LLM API key for query processing and auto-descriptions |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Yes (has default) | OpenRouter API endpoint |
| `AWS_ACCESS_KEY_ID` | Your AWS access key | **Yes** | S3 access for file upload/download/delete |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key | **Yes** | S3 access (paired with access key ID) |
| `AWS_REGION` | `us-east-1` (or your bucket's region) | **Yes** | S3 bucket region — must match where `ragsheets` bucket was created |
| `S3_BUCKET` | `ragsheets` | Yes (has default) | S3 bucket name for Excel file storage |
| `CORS_ORIGINS` | `https://ragsheets-frontend.up.railway.app` | **Yes** | Allowed frontend origins (comma-separated) |
| `DATA_DIR` | `/app/data` | **Yes** | Persistent volume mount path for SQLite DB |
| `UPLOAD_FOLDER` | `/tmp/uploads` | Yes (has default) | Temp directory for S3 downloads during queries |
| `REDIS_URL` | (from Railway Redis plugin) | Optional | Redis connection string for LLM cache |

**Note**: The `.env` file is gitignored and will NOT be available on Railway. All secrets must be set via the dashboard. This is the correct approach — `.env` files are for local development only. In production, secrets come from the platform's secret manager (Railway's env vars are encrypted at rest).

**Why AWS credentials are needed:**
- `boto3.client("s3")` in `sheet_metadata.py` uses the standard AWS SDK credential chain: `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env vars → `~/.aws/credentials` file → IAM role. Railway containers don't have `~/.aws/credentials`, so env vars are the only option.
- **Least privilege**: The IAM user should only have `s3:PutObject`, `s3:GetObject`, and `s3:DeleteObject` on the `ragsheets` bucket. Do not use full S3 access or root credentials.

**Why `DATA_DIR` is required (not optional):**
- Without it, the app defaults to writing `sheets.db` in the source directory (`/app/src/`). This works but is fragile — if the volume is later mounted at `/app/src/`, it would shadow the source code. Setting `DATA_DIR=/app/data` explicitly ensures the DB always goes to the volume, even if the volume mount path changes.

### 2.3 Backend service — persistent volume

1. Backend service → Settings → Volumes → Add Volume
2. Mount path: `/app/data`
3. Size: 1 GB (sufficient for SQLite metadata; S3 stores the actual Excel files)

**Why 1 GB:**
- Each sheet metadata record is ~1KB (file name, sheet name, fields list, descriptions). 1 GB holds ~1 million sheet records — far more than needed.
- The LLM cache adds ~2-5KB per entry (response text + embedding vector). Even with 10K cached responses, that's only ~50MB.
- 1 GB is the minimum Railway volume size. There's no cost savings from choosing smaller.
- **Monitoring**: Check volume usage via `GET /api/storage/stats` — if usage exceeds 80%, increase the volume size.

**What happens if the volume is full:**
- SQLite will return `SQLITE_FULL` errors on writes. The app will return 500 errors on upload/describe operations.
- Reads will still work (queries use cached data or S3).
- Fix: increase volume size in Railway dashboard (no redeploy needed — Railway expands volumes online).

### 2.4 Frontend service — environment variables

No runtime env vars needed with Option A (private networking). The frontend is static files served by nginx — all configuration is baked in at build time.

**Why no runtime env vars for the frontend:**
- The frontend is a static SPA. There's no server-side process to read env vars at runtime.
- Vite inlines `VITE_API_ENDPOINT` at build time. The value `/api` is environment-agnostic.
- nginx config is static (uses `backend.railway.internal`, which is resolved by Railway's DNS).
- This is a significant advantage: the frontend container is completely stateless and immutable. It can be scaled horizontally with zero configuration.

If using Option B (public URL proxy), set:
| Variable | Value |
|----------|-------|
| `BACKEND_URL` | `https://ragsheets-backend.up.railway.app` |

### 2.5 Add Redis (optional but recommended)

1. Railway project → New → Database → Add Redis
2. Railway provides a `REDIS_URL` connection string
3. Reference it in the backend service variables: `REDIS_URL=${{Redis.REDIS_URL}}`

**When to add Redis:**

| Scenario | Without Redis | With Redis |
|----------|--------------|------------|
| Single user, <10 queries/day | ✅ SQLite fallback works fine | No meaningful benefit |
| Multiple users, concurrent queries | ⚠️ SQLite single-writer lock contention | ✅ Redis handles concurrent reads/writes |
| Semantic caching (embedding similarity) | ⚠️ O(n) scan of all embeddings in SQLite | ✅ O(log n) vector search via RediSearch |
| Cache expiry | ⚠️ Daily cron job (`cleanup_old_cache_entries`) | ✅ Native TTL (7-day auto-expiry) |

**Why not require Redis from the start:**
- The app has a graceful SQLite fallback (`cache_service.py` checks for Redis, falls back to SQLite). Adding Redis later requires zero code changes — just set `REDIS_URL`.
- Redis adds ~$0/month on Railway's free tier (limited usage) but adds operational complexity (another service to monitor).
- For a capstone demo or low-traffic production, SQLite is sufficient. Add Redis when you expect >5 concurrent users.

**Railway Redis vs Upstash Redis:**
- **Railway Redis**: Built-in plugin, same dashboard, private networking. Easier to manage.
- **Upstash Redis**: External serverless Redis, free tier (10K commands/day), pay-per-request. Better for sparse workloads.
- **Recommendation**: Start with Railway Redis (simplicity). Switch to Upstash if cost becomes an issue at scale.

### 2.6 Custom domains (optional but recommended for production)

1. Frontend service → Settings → Networking → Generate Domain (or connect custom domain)
2. Backend service → Settings → Networking → Generate Domain (needed for health checks; or keep private if using Option A)
3. Update `CORS_ORIGINS` on backend to match the frontend domain

**Why generate domains:**
- Railway's auto-generated domains (`*.up.railway.app`) are fine for staging. For production, use a custom domain (e.g., `ragsheets.yourdomain.com`) for:
  - **Professional appearance**: Users see your domain, not Railway's.
  - **Portability**: If you leave Railway, you point the domain elsewhere. No broken links.
  - **HTTPS**: Railway provides automatic Let's Encrypt certificates for both generated and custom domains.

**Why the backend may not need a public domain:**
- With Option A (private networking), the frontend proxies to `backend.railway.internal:8000`. The backend is never accessed directly by the browser.
- However, Railway needs a public domain for HTTP health checks. Alternatively, use the TCP health check probe (Railway pings the port) instead of an HTTP health check.

---

## Phase 3: Pre-deploy Verification Checklist

### Code changes
- [x] `frontend/ragsheets/.env.production` → `VITE_API_ENDPOINT="/api"`
- [x] `frontend/default.conf` → `proxy_pass http://backend.railway.internal:8000`
- [x] `backend/src/sheet_metadata.py` → `DATA_DIR` env var for `DB_PATH`
- [x] `backend/src/main.py` → `UPLOAD_FOLDER` env var
- [x] `backend/src/main.py` → CORS defaults updated (removed Duke VM)
- [x] `backend/railway.toml` created (build + deploy config)
- [x] `frontend/railway.toml` created (build + deploy config)

### Railway dashboard
- [ ] Backend service created with all env vars (see §2.2)
- [ ] Backend persistent volume mounted at `/app/data`
- [ ] Frontend service created
- [ ] `CORS_ORIGINS` set to frontend's Railway domain
- [ ] AWS credentials set (S3 access for file upload/download)
- [ ] IAM user has least-privilege policy (only `s3:PutObject`, `s3:GetObject`, `s3:DeleteObject` on `ragsheets` bucket)
- [ ] Redis plugin added (optional, recommended for production)
- [ ] Frontend domain generated or custom domain connected
- [ ] S3 lifecycle policy applied (`s3_lifecycle.json` — 90-day expiry)

### Post-deploy smoke tests
- [ ] Frontend loads at Railway URL (check browser console for errors)
- [ ] Upload tab renders, file upload works (S3 upload succeeds — check backend logs for "Uploaded to S3")
- [ ] Sheet descriptions save and persist after page refresh
- [ ] Query tab works — submit a query, get a response (check backend logs for LLM call)
- [ ] Delete file works (S3 + SQLite cleanup — verify file disappears from UI and S3)
- [ ] **Volume persistence test**: Redeploy backend (make a trivial change and push) → verify uploaded files still appear in the UI (sheets.db survived redeploy)
- [ ] Check backend logs for any `load_dotenv()` or missing env var errors
- [ ] Verify nginx proxy: open browser dev tools → Network tab → confirm `/api/sheets/` returns 200 (not 502/504)
- [ ] Verify CORS: if frontend and backend have different domains, check that API responses include `Access-Control-Allow-Origin` header

---

## Phase 4: Known Limitations & Future Improvements

### SQLite on Railway

SQLite works with a persistent volume but has limitations:

| Limitation | Impact | Mitigation |
|-----------|--------|------------|
| **Single-writer** | Concurrent writes serialize (fine for <5 concurrent users) | Add Redis for cache writes; SQLite only handles metadata |
| **No multi-region replication** | Volume is tied to one container instance | Migrate to PostgreSQL when multi-region is needed |
| **No automatic backup** | Volume failure = data loss | Set up Railway volume snapshots or `sqlite3 .dump` cron job |
| **No connection pooling** | Each request opens/closes a connection | Current code uses `_get_db()` per request — acceptable for SQLite (no network overhead) |

**When to migrate to PostgreSQL:**
- More than 5 concurrent users (SQLite write lock contention causes timeouts)
- Need for multi-instance backend scaling (multiple containers can't share a SQLite file)
- Need for automatic backups (PostgreSQL has built-in WAL archiving)
- Need for ACID transactions across multiple tables (SQLite has this, but PostgreSQL handles concurrent transactions better)

**Migration path (when needed):**
1. Provision Railway PostgreSQL plugin
2. Update `sheet_metadata.py` to use `psycopg2` instead of `sqlite3`
3. Convert SQLite-specific syntax (`INSERT OR REPLACE` → `INSERT ... ON CONFLICT DO UPDATE`)
4. Run schema migration script
5. Set `DATABASE_URL` env var
6. No frontend changes needed — API contract is unchanged

### Temp file cleanup

Downloaded S3 files in `/tmp/uploads` accumulate during query processing. The daily cleanup cron (APScheduler) handles SQLite metadata, but not temp files. Add a cleanup step:

```python
# In daily_cleanup() — delete temp files older than 1 day
import glob, time
for f in glob.glob(os.path.join(UPLOAD_FOLDER, "temp_*")):
    if os.path.getmtime(f) < time.time() - 86400:
        os.remove(f)
```

**Why this matters:**
- Each query downloads Excel files from S3 to `/tmp/uploads/temp_{file_id}_{filename}`. These files are 1-50MB each.
- Without cleanup, `/tmp` fills up over time, causing the container to crash with "No space left on device."
- The APScheduler cron job already runs at 3 AM daily — adding temp file cleanup to the same job is zero-cost.

### Health check endpoint

Railway supports health checks. Add a simple endpoint:

```python
@app.get("/health")
async def health():
    return {"status": "ok"}
```

Configure in Railway: backend service → Settings → Health Check → path: `/api/health`

**Why health checks matter:**
- Railway uses health checks to determine if a container is ready to receive traffic. Without a health check, Railway routes traffic to the container immediately after it starts — but the app may still be initializing (loading models, connecting to S3, etc.).
- If the health check fails, Railway automatically restarts the container. This catches issues like missing env vars, failed DB initialization, or crashed workers.
- The health check should be lightweight (no S3 or LLM calls) — just verify the process is alive and responsive.

**Enhanced health check (future):**
```python
@app.get("/health")
async def health():
    checks = {
        "db": _check_db(),          # Can we read from sheets.db?
        "s3": _check_s3(),          # Can we list the S3 bucket?
        "redis": _check_redis(),    # Is Redis connected? (optional)
    }
    all_ok = all(checks.values())
    return {"status": "ok" if all_ok else "degraded", "checks": checks}
```

### Nginx proxy timeout

**Current issue**: The backend's LLM queries can take up to 120 seconds (set via `timeout: 120000` in the frontend's axios calls and the backend's own processing time). Nginx's default `proxy_read_timeout` is 60 seconds. If a query takes longer than 60 seconds, nginx returns `504 Gateway Timeout` to the browser, even though the backend is still processing.

**Fix**: Add to `frontend/default.conf` in the `location /api` block:

```nginx
proxy_read_timeout 120s;
proxy_send_timeout 120s;
```

**Why this matters:**
- LLM queries (especially multi-sheet calculations) can take 30-90 seconds. The 60s default will cause intermittent 504 errors.
- The frontend already handles timeouts gracefully (axios timeout at 120s), but nginx sits between the browser and backend. If nginx times out first, the browser gets a 504 instead of the actual response.

### Sentence-transformers model download

The semantic cache uses `Qwen/Qwen3-Embedding-8B` (~16GB). On first deploy, this model will download on container startup, causing a slow first boot (10-30 minutes depending on bandwidth).

**Options (in order of recommendation):**

1. **Skip semantic caching initially** (recommended for first deploy)
   - Set `semantic_cache_available` to return `False` if the model is not present
   - The app works without semantic caching — only exact-match caching is affected
   - Add semantic caching later once the deployment is stable

2. **Use a smaller embedding model** (recommended for production)
   - `all-MiniLM-L6-v2` (~80MB) provides good semantic matching for 90% of use cases
   - Download time: <5 seconds on first boot
   - Tradeoff: slightly lower semantic accuracy (0.88 vs 0.92 threshold needed)
   - Change one line in `semantic_cache.py`: `SentenceTransformer("all-MiniLM-L6-v2")`

3. **Pre-download in Dockerfile** (not recommended)
   - Adds ~16GB to the Docker image
   - Railway build would take 30+ minutes and may hit storage limits
   - Every code change triggers a full rebuild with the 16GB layer

**Why this matters:**
- Railway containers have a startup grace period (usually 5-10 minutes). If the app doesn't respond to health checks within that window, Railway kills and restarts the container — creating an infinite restart loop.
- The model download happens on first boot only (HuggingFace caches to `~/.cache/huggingface/`). But since Railway containers are ephemeral, every redeploy re-downloads the model unless the cache is on a persistent volume.

### Logging and observability

**Current state**: The app uses `print()` statements for logging. Railway captures stdout/stderr and displays it in the dashboard.

**Recommendations for production:**

1. **Replace `print()` with structured logging** (future improvement):
   ```python
   import logging
   logger = logging.getLogger("ragsheets")
   logger.info("Sheet uploaded", extra={"file_id": file_id, "sheet_count": len(metas)})
   ```
   Structured logs are easier to search and filter in Railway's log viewer.

2. **Add request ID for tracing** (future improvement):
   - Generate a UUID per request, include it in logs and response headers
   - When debugging a failed query, search for the request ID to see all related log entries

3. **Monitor key metrics** (via existing endpoints):
   - `GET /api/storage/stats` — file count, sheet count, cache stats
   - `GET /api/cache/stats` — cache hit rate, entry count
   - Set up Railway alerts for: high memory usage, high CPU usage, failed health checks

### Security considerations

| Concern | Current state | Recommendation |
|---------|--------------|----------------|
| **API keys in `.env`** | ✅ Gitignored, set via Railway env vars | Good — never commit secrets |
| **OpenRouter API key** | ✅ Read from env var, not hardcoded | Good |
| **AWS credentials** | ✅ Read from env vars via boto3 | Use least-privilege IAM user |
| **CORS** | ✅ Configurable via env var, no `*` in production | Set to exact frontend domain |
| **HTTPS** | ✅ Railway provides automatic TLS | Good — no config needed |
| **Input validation** | ⚠️ File upload checks extension but not content | Add magic number validation for .xlsx files |
| **Rate limiting** | ❌ Not implemented | Add `slowapi` or Railway's rate limiting for public endpoints |
| **Authentication** | ❌ No auth — anyone can upload/query | Add API key or OAuth for production multi-user use |

**Why authentication is not needed for initial deployment:**
- The app is a capstone project demo. The Railway URL is not widely known.
- Adding auth (OAuth, JWT, etc.) is a significant feature addition that changes the UX. It should be a separate planned effort, not a deployment blocker.
- **When auth becomes needed**: When the app is publicly announced, when multiple users need isolated data, or when the S3/OpenRouter costs need to be attributed to users.

---

## 12-Factor App Compliance

This deployment config follows the [12-Factor App](https://12factor.net) methodology:

| Factor | Compliance | Notes |
|--------|-----------|-------|
| **I. Codebase** | ✅ | One codebase, multiple deploys (local, Railway staging, Railway prod) |
| **II. Dependencies** | ✅ | `requirements.txt` and `package.json` explicitly declare all dependencies |
| **III. Config** | ✅ | All environment-specific values are in env vars (not hardcoded) |
| **IV. Backing services** | ✅ | S3, OpenRouter, Redis are all addressable via env vars |
| **V. Build, release, run** | ✅ | Docker builds are separate from runtime. Railway handles release promotion |
| **VI. Processes** | ✅ | App is stateless — all state is in S3, SQLite (volume), or Redis |
| **VII. Port binding** | ✅ | App listens on `PORT` env var (Railway sets automatically) |
| **VIII. Concurrency** | ⚠️ | Single uvicorn worker. Add `--workers 4` for production (but SQLite single-writer limits this) |
| **IX. Disposability** | ✅ | App starts quickly, handles SIGTERM gracefully (FastAPI lifespan) |
| **X. Dev/prod parity** | ✅ | Same Docker images for dev and prod. Only env vars differ |
| **XI. Logs** | ⚠️ | Uses `print()` — works on Railway but should be structured logging |
| **XII. Admin processes** | ✅ | APScheduler runs daily cleanup as part of the app process |

---

## File Change Summary

| File | Change | Status |
|------|--------|--------|
| `frontend/ragsheets/.env.production` | Change API URL to `/api` | ✅ Done |
| `frontend/default.conf` | Change `proxy_pass` to Railway internal URL | ✅ Done |
| `backend/src/sheet_metadata.py` | Add `DATA_DIR` env var for `DB_PATH` | ✅ Done |
| `backend/src/main.py` | Add `UPLOAD_FOLDER` env var | ✅ Done |
| `backend/src/main.py` | Update CORS defaults (remove Duke VM) | ✅ Done |
| `backend/railway.toml` | New file — backend build/deploy config | ✅ Done |
| `frontend/railway.toml` | New file — frontend build/deploy config | ✅ Done |
| `plans/deployment.md` | This document | ✅ Done |

## Recommended Future Changes (Not Blocking Deployment)

| File | Change | Priority | Rationale |
|------|--------|----------|-----------|
| `frontend/default.conf` | Add `proxy_read_timeout 120s` | **High** | Prevents 504 Gateway Timeout on long LLM queries |
| `backend/src/semantic_cache.py` | Switch to `all-MiniLM-L6-v2` or skip model load | **High** | Prevents 30-min startup on first deploy (16GB model download) |
| `backend/src/main.py` | Add temp file cleanup to `daily_cleanup()` | **Medium** | Prevents `/tmp` filling up with downloaded S3 files |
| `backend/Dockerfile` | Add `--workers 2` to uvicorn CMD | **Low** | Improves concurrency (but SQLite single-writer limits benefit) |
| `backend/src/main.py` | Replace `print()` with `logging` | **Low** | Structured logs are easier to search in Railway dashboard |

---

## CI/CD Pipeline

### Overview

The CI/CD pipeline uses **GitHub Actions** to automate testing, deployment, and monitoring. The pipeline ensures that only code passing all tests reaches production, and that the production service is continuously monitored for uptime.

```
Push to agentic branch
  │
  ▼
┌─────────────────────────────────┐
│  CI Workflow (ci.yml)           │
│  ├── Backend Tests (pytest)     │
│  └── Frontend Build (tsc+vite)  │
└─────────────────────────────────┘
  │ All jobs pass?
  ├── No → ❌ Block deploy (Railway checkSuites)
  └── Yes
      ▼
┌─────────────────────────────────┐
│  Deploy Workflow (deploy.yml)   │
│  ├── railway up --detach        │
│  └── Health check /health       │
└─────────────────────────────────┘
  │
  ▼
┌─────────────────────────────────┐
│  Monitor Workflow (monitor.yml) │
│  Runs every 10 minutes          │
│  ├── Check /health endpoint     │
│  ├── Check frontend loads       │
│  └── Create GitHub issue on fail│
└─────────────────────────────────┘
```

### Workflows

#### 1. CI (`ci.yml`) — Test & Build Gate

**Triggers:** Push or PR to `agentic` branch.

**Jobs:**
- **Backend Tests**: Installs Python 3.12 + `backend/requirements.txt`, runs `pytest` excluding tests that require a running server (`test_query_pipeline`, `test_frontend_integration`), external API keys (`test_real_agent_codegen`), or Redis (`test_cache_service`).
- **Frontend Build**: Installs Node 22, runs `npm ci`, `tsc --noEmit` (type check), and `npm run build` (Vite production build).

**Concurrency:** Cancels in-progress CI runs for the same branch when a new push arrives.

#### 2. Deploy (`deploy.yml`) — Deploy on Green

**Trigger:** `workflow_run` on CI workflow completion — fires only when CI succeeds on `agentic`.

**Steps:**
1. Checks out the exact commit that CI validated (`github.event.workflow_run.head_sha`).
2. Installs Railway CLI.
3. Runs `railway up --detach` to deploy to Railway production.
4. Polls `/health` endpoint for up to 2.5 minutes (10 attempts × 15s) to confirm the deployment is live.

**Required GitHub Secrets:**
- `RAILWAY_TOKEN` — Railway API token (generate at railway.app → Account Settings → API Tokens)
- `RAILWAY_PROJECT_ID` — Railway project ID (find in `railway status --json`)

#### 3. Monitor (`monitor.yml`) — Continuous Health Checks

**Triggers:** Every 10 minutes via `cron`, or manual dispatch.

**Steps:**
1. Checks `https://excel-chat-production-76dc.up.railway.app/health` for HTTP 200.
2. Checks `https://excel-chat-production-76dc.up.railway.app` (frontend) for HTTP 200.
3. If either fails, creates a GitHub issue labeled `monitoring` + `production-down` (deduplicated — won't create duplicate issues if one is already open).

### Railway Configuration Changes

| Setting | Value | Purpose |
|---------|-------|---------|
| `source.rootDirectory` | `excel-chat` | Ensures Dockerfile paths resolve to the correct subdirectory |
| `source.checkSuites` | `true` | Railway waits for GitHub Actions CI to pass before auto-deploying |
| `deploy.healthcheckPath` | `/health` | Railway polls this path after deploy; auto-restarts on failure |

### Setup Instructions

1. **Add GitHub Secrets:**
   - Go to repo Settings → Secrets and variables → Actions → New repository secret
   - `RAILWAY_TOKEN`: Generate at railway.app → Account Settings → API Tokens
   - `RAILWAY_PROJECT_ID`: Run `railway status --json` locally, copy the project ID

2. **Verify Railway settings:**
   ```bash
   railway status --json  # Confirm rootDirectory=excel-chat, checkSuites=true
   ```

3. **Test the pipeline:**
   - Push a small change to `agentic` branch
   - Watch GitHub Actions tab — CI should run, then Deploy should trigger
   - Check Monitor workflow runs on schedule

### Excluded Tests (CI)

These tests are excluded from CI because they require external services:

| Test File | Reason |
|-----------|--------|
| `test_query_pipeline.py` | Requires running backend server on localhost:8000 |
| `test_frontend_integration.py` | Requires running frontend + backend |
| `test_real_agent_codegen.py` | Requires `OPENROUTER_API_KEY` for live LLM calls |
| `test_cache_service.py` | Requires Redis instance |

These tests can be run locally with the appropriate services running.
