# Railway Deployment Plan

## Architecture Overview

```
Railway Project
├── Frontend Service (nginx + React static files)
│   ├── Built from frontend/Dockerfile
│   ├── Serves static files on port 80
│   └── Proxies /api/* to backend service
│
├── Backend Service (FastAPI + uvicorn)
│   ├── Built from backend/Dockerfile
│   ├── Runs on PORT env var (Railway sets automatically)
│   └── Connects to: S3, OpenRouter, Redis (optional)
│
├── Redis Service (Railway plugin — optional)
│   └── Used for LLM response caching
│
└── Persistent Volume
    └── Mounted to backend at /app/data
        └── Stores sheets.db (SQLite metadata + cache fallback)
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

### 1.2 Fix nginx proxy target for Railway

**File**: `frontend/default.conf`

Railway services communicate via internal URLs, not Docker Compose service names. Replace the hardcoded `http://backend:8000` with an env-substituted variable.

```nginx
location /api {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header ORIGIN $http_origin;
    add_header Access-Control-Allow-Origin $http_origin;
    expires -1;
}
```

**Wait — this won't work on Railway either.** Railway runs each service in its own container. The frontend container can't reach `127.0.0.1:8000` because the backend is in a separate container.

**Two options:**

**Option A (recommended): Use Railway's private networking**
Railway assigns each service a private IPv4 address (e.g., `172.16.x.x`). You can reference it via the service's internal hostname.

```nginx
location /api {
    proxy_pass http://backend.railway.internal:8000;
    proxy_set_header Host $host;
    proxy_set_header ORIGIN $http_origin;
    add_header Access-Control-Allow-Origin $http_origin;
    expires -1;
}
```

Railway's internal DNS resolves `backend.railway.internal` to the backend service's private IP. This requires both services to be in the same Railway project and private networking enabled (enabled by default).

**Option B (simpler, less secure): Use the backend's public Railway URL**
Set the backend URL as a build-time env var and substitute it in nginx config via `envsubst` in the Dockerfile entrypoint.

```dockerfile
# frontend/Dockerfile — add at the end
CMD ["sh", "-c", "envsubst < /etc/nginx/conf.d/default.conf.template > /etc/nginx/conf.d/default.conf && nginx -g 'daemon off;'"]
```

Rename `default.conf` to `default.conf.template` and use:
```nginx
proxy_pass ${BACKEND_URL};
```

Then set `BACKEND_URL` in Railway's frontend service variables to the backend's public URL (e.g., `https://ragsheets-backend.up.railway.app`).

**Decision**: Go with **Option A** (private networking). It's simpler — no Dockerfile changes, no envsubst, and traffic stays internal.

### 1.3 Add persistent volume for SQLite

**File**: `backend/src/sheet_metadata.py`

Currently:
```python
DB_PATH = os.path.join(os.path.dirname(__file__), "sheets.db")
```

Change to use a data directory that will be mounted as a volume:
```python
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "sheets.db")
```

**Railway setup**: Add a persistent volume mounted at `/app/data` and set `DATA_DIR=/app/data`.

**Why**: Railway containers are ephemeral. Without a volume, `sheets.db` is wiped on every redeploy, losing all file metadata, sheet descriptions, and cache entries.

### 1.4 Fix UPLOAD_FOLDER for temp files

**File**: `backend/src/main.py`

Currently:
```python
UPLOAD_FOLDER = './uploads'
```

Change to use the same data directory or a temp directory that doesn't need persistence:
```python
UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/tmp/uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
```

**Why**: Temp files downloaded from S3 for query processing don't need to survive redeploys. Using `/tmp` is fine — these are transient. If we used the volume, we'd waste storage on temp files.

### 1.5 Update CORS origins default

**File**: `backend/src/main.py`

Currently:
```python
_default_origins = "http://localhost:5173,http://vmm-45508.vm.duke.edu"
```

This is fine as a default since it's overridable via `CORS_ORIGINS` env var. But document that you must set `CORS_ORIGINS` in Railway to include the frontend's Railway domain.

### 1.6 Create `railway.toml`

**File**: `railway.toml` (repo root)

```toml
[services.backend]
root = "backend"
dockerfilePath = "Dockerfile"

[services.frontend]
root = "frontend"
dockerfilePath = "Dockerfile"
```

This tells Railway to create two services from one repo, each building from its respective Dockerfile.

---

## Phase 2: Railway Dashboard Configuration

### 2.1 Create Railway project

1. Go to [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
2. Select the `excel-chat` repo
3. Railway detects `railway.toml` and creates two services: `backend` and `frontend`

### 2.2 Backend service — environment variables

Set these in the Railway dashboard under the backend service → Variables:

| Variable | Value | Required |
|----------|-------|----------|
| `OPENROUTER_API_KEY` | `sk-or-v1-...` (from `.env`) | **Yes** |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Yes (has default) |
| `AWS_ACCESS_KEY_ID` | Your AWS access key | **Yes** (S3 access) |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key | **Yes** (S3 access) |
| `AWS_REGION` | `us-east-1` (or your bucket's region) | **Yes** |
| `S3_BUCKET` | `ragsheets` | Yes (has default) |
| `CORS_ORIGINS` | `https://ragsheets-frontend.up.railway.app` | **Yes** (your frontend URL) |
| `DATA_DIR` | `/app/data` | **Yes** (volume mount path) |
| `UPLOAD_FOLDER` | `/tmp/uploads` | Yes (has default) |
| `REDIS_URL` | (from Railway Redis plugin) | Optional |

**Note**: The `.env` file is gitignored and will NOT be available on Railway. All secrets must be set via the dashboard.

### 2.3 Backend service — persistent volume

1. Backend service → Settings → Volumes → Add Volume
2. Mount path: `/app/data`
3. Size: 1 GB (sufficient for SQLite metadata; S3 stores the actual Excel files)

### 2.4 Frontend service — environment variables

No runtime env vars needed. The frontend is static files served by nginx.

If using Option B (public URL proxy), set:
| Variable | Value |
|----------|-------|
| `BACKEND_URL` | `https://ragsheets-backend.up.railway.app` |

With Option A (private networking), no env vars needed.

### 2.5 Add Redis (optional)

1. Railway project → New → Database → Add Redis
2. Railway provides a `REDIS_URL` connection string
3. Reference it in the backend service variables: `REDIS_URL=${{Redis.REDIS_URL}}`

**When to add**: Only if you expect concurrent users. SQLite fallback works fine for single-user or low-traffic scenarios.

### 2.6 Custom domains (optional)

1. Frontend service → Settings → Networking → Generate Domain
2. Backend service → Settings → Networking → Generate Domain (or keep private)
3. Update `CORS_ORIGINS` on backend to match the frontend domain

---

## Phase 3: Pre-deploy Verification Checklist

### Code changes
- [ ] `frontend/ragsheets/.env.production` → `VITE_API_ENDPOINT="/api"`
- [ ] `frontend/default.conf` → `proxy_pass http://backend.railway.internal:8000`
- [ ] `backend/src/sheet_metadata.py` → `DATA_DIR` env var for `DB_PATH`
- [ ] `backend/src/main.py` → `UPLOAD_FOLDER` env var
- [ ] `railway.toml` created at repo root

### Railway dashboard
- [ ] Backend service created with all env vars (see §2.2)
- [ ] Backend persistent volume mounted at `/app/data`
- [ ] Frontend service created
- [ ] `CORS_ORIGINS` set to frontend's Railway domain
- [ ] AWS credentials set (S3 access for file upload/download)
- [ ] Redis plugin added (optional, recommended for production)

### Post-deploy smoke tests
- [ ] Frontend loads at Railway URL
- [ ] Upload tab renders, file upload works (S3 upload succeeds)
- [ ] Sheet descriptions save and persist after page refresh
- [ ] Query tab works — submit a query, get a response
- [ ] Delete file works (S3 + SQLite cleanup)
- [ ] Redeploy backend → verify `sheets.db` persists (volume works)
- [ ] Check backend logs for any `load_dotenv()` or missing env var errors

---

## Phase 4: Known Limitations & Future Improvements

### SQLite on Railway
SQLite works with a persistent volume but has limitations:
- **Single-writer**: concurrent writes serialize (fine for low traffic)
- **No multi-region replication**: the volume is tied to one container instance
- **Backup**: no automatic backup — must manually snapshot or export

**Future**: Migrate to Railway's managed PostgreSQL when:
- More than 5 concurrent users
- Need for multi-instance backend scaling
- Need automatic backups

### Temp file cleanup
Downloaded S3 files in `/tmp/uploads` accumulate during query processing. The daily cleanup cron (APScheduler) handles SQLite metadata, but not temp files. Add a cleanup step:

```python
# In daily_cleanup() — delete temp files older than 1 day
import glob, time
for f in glob.glob(os.path.join(UPLOAD_FOLDER, "temp_*")):
    if os.path.getmtime(f) < time.time() - 86400:
        os.remove(f)
```

### Health check endpoint
Railway supports health checks. Add a simple endpoint:

```python
@app.get("/health")
async def health():
    return {"status": "ok"}
```

Configure in Railway: backend service → Settings → Health Check → path: `/api/health`

### Sentence-transformers model download
The semantic cache uses `Qwen/Qwen3-Embedding-8B` (~16GB). On first deploy, this model will download on container startup, causing a slow first boot. Options:
- Pre-download the model in the Dockerfile (increases image size by ~16GB)
- Use a smaller embedding model for production (e.g., `all-MiniLM-L6-v2` at ~80MB)
- Skip semantic caching initially (set `semantic_cache_available` to return False if model not present)

---

## File Change Summary

| File | Change | Lines |
|------|--------|-------|
| `frontend/ragsheets/.env.production` | Change API URL to `/api` | 1 |
| `frontend/default.conf` | Change `proxy_pass` to Railway internal URL | 22 |
| `backend/src/sheet_metadata.py` | Add `DATA_DIR` env var for `DB_PATH` | 20-21 |
| `backend/src/main.py` | Add `UPLOAD_FOLDER` env var | 119 |
| `railway.toml` | New file — multi-service config | new |
