from fastapi import FastAPI, Header, UploadFile, HTTPException
from excelservices import ExcelService
# from vectordbservices import VectorDBService  # ChromaDB disabled
from queryservices import QueryService
import pandas as pd
from io import StringIO, BytesIO
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipeline import build_query_pipeline, generate_user_friendly_response
import os
import uuid
from contextlib import asynccontextmanager
from openai import OpenAI
from llama_index.core.prompts import PromptTemplate
from llama_index.core.llms import ChatMessage, MessageRole
from dotenv import load_dotenv
from pathlib import Path
from classification_template import ClassTemplates
from sheet_metadata import (
    init_db, save_file, save_sheet, get_all_sheets, get_all_files,
    update_sheet_description, delete_file, get_sheets_by_file,
    upload_to_s3, download_from_s3, delete_from_s3, SheetMeta,
    get_cache_stats, cleanup_old_cache_entries,
    touch_file_access, find_stale_files, delete_files_older_than,
    invalidate_user_cache as sqlite_invalidate_user_cache,
)
from semantic_cache import (
    embed_query, find_similar_cached, store_cached,
    invalidate_user_cache as semantic_invalidate_user_cache,
    is_available as semantic_cache_available,
)
from pydantic import BaseModel as PydanticBaseModel

load_dotenv()


# ---------------------------------------------------------------------------
# Application-level cron: APScheduler runs daily_cleanup() at 3 AM. This pairs
# with the S3 lifecycle policy (uploads/* expires after 90 days) — when S3
# removes an object, the SQLite metadata becomes orphaned and we need to drop
# the matching files + sheets rows.
# ---------------------------------------------------------------------------
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    _SCHEDULER_AVAILABLE = True
except ImportError:  # pragma: no cover
    AsyncIOScheduler = None  # type: ignore
    CronTrigger = None  # type: ignore
    _SCHEDULER_AVAILABLE = False


def daily_cleanup(max_age_days: int = 90, cache_max_age_days: int = 7) -> dict:
    """Application-level daily cleanup job.

    1. Deletes SQLite metadata for files older than ``max_age_days`` days.
       By the time these records are removed, the S3 lifecycle policy will
       already have expired the underlying objects (uploads/* → expire 90d).
    2. Purges LLM cache entries not accessed in ``cache_max_age_days`` days.
       This is the SQLite-only fallback; Redis TTL handles expiry natively
       when REDIS_URL is configured.

    Returns a dict with counts so the caller can log or return it to clients.
    """
    deleted_file_ids = delete_files_older_than(max_age_days)
    deleted_cache = cleanup_old_cache_entries(cache_max_age_days)
    print(
        f"✓ daily_cleanup: removed {len(deleted_file_ids)} old file record(s), "
        f"{deleted_cache} stale cache entries."
    )
    return {
        "deleted_files": deleted_file_ids,
        "deleted_files_count": len(deleted_file_ids),
        "deleted_cache_entries": deleted_cache,
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup/shutdown hook for the cleanup cron."""
    if _SCHEDULER_AVAILABLE:
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            daily_cleanup,
            CronTrigger(hour=3, minute=0),
            id="daily_cleanup",
            replace_existing=True,
        )
        scheduler.start()
        app.state.scheduler = scheduler
        print("✓ Daily cleanup scheduler started (3 AM daily).")
    else:
        app.state.scheduler = None
        print("⚠️ APScheduler not installed; daily cleanup will not run automatically.")
    try:
        yield
    finally:
        if app.state.scheduler is not None:
            app.state.scheduler.shutdown(wait=False)


app = FastAPI(root_path='/api', lifespan=_lifespan)

# Initialize the SQLite database on startup
init_db()

# list of allowed origins (configurable via env, comma-separated)
_default_origins = "http://localhost:5173,http://localhost:3000"
origins = [o.strip() for o in os.environ.get("CORS_ORIGINS", _default_origins).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_FOLDER = './uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ============================================================================
# File Upload (S3 + Multi-Sheet)
# ============================================================================

@app.post("/upload/")
async def create_upload_file(
    excelFile: UploadFile,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    user_id = x_user_id or "anonymous"
    print(f"Received file upload request. Filename: {excelFile.filename}, user_id={user_id}")
    if not excelFile.filename:
        return JSONResponse(content={"message": "No file provided"}, status_code=400)
    if not excelFile.filename.endswith(('.xlsx', '.xls', '.numbers')):
        return JSONResponse(content={"message": f"Invalid file format: {excelFile.filename}. Please upload .xlsx, .xls, or .numbers files"}, status_code=400)

    # Save locally temporarily
    filepath = os.path.join(UPLOAD_FOLDER, excelFile.filename)
    with open(filepath, "wb") as f:
        while chunk := await excelFile.read(1024 * 1024):
            f.write(chunk)

    # Generate IDs
    file_id = str(uuid.uuid4())
    s3_key = f"uploads/{file_id}/{excelFile.filename}"

    # Upload to S3
    try:
        upload_to_s3(filepath, s3_key)
        print(f"Uploaded to S3: {s3_key}")
    except Exception as e:
        print(f"⚠️ S3 upload failed: {e}")
        return JSONResponse(content={"message": f"Upload failed: {e}"}, status_code=500)

    # Load all sheets and create metadata
    try:
        sheet_metas = ExcelService.load_sheet_metadata_from_file(filepath, file_id, excelFile.filename, s3_key)
        print(f"Found {len(sheet_metas)} sheets in {excelFile.filename}")

        # Detect schema groups
        ExcelService.detect_schema_groups(sheet_metas)
        print(f"Schema groups: {set(m.schema_group for m in sheet_metas)}")

        # Save file record
        save_file(file_id, excelFile.filename, s3_key, len(sheet_metas), user_id=user_id)

        # Save each sheet metadata
        for meta in sheet_metas:
            save_sheet(meta, user_id=user_id)

        # Auto-describe all sheets via LLM (async, non-blocking for response)
        # We do this synchronously for now so descriptions are ready immediately
        try:
            ExcelService.auto_describe_all_sheets(sheet_metas)
        except Exception as e:
            print(f"⚠️ Auto-description failed: {e}")

    except Exception as e:
        print(f"⚠️ Sheet processing failed: {e}")
        import traceback
        traceback.print_exc()
        return JSONResponse(content={"message": f"File uploaded but sheet processing failed: {e}"}, status_code=500)
    finally:
        # Clean up local file
        if os.path.exists(filepath):
            os.remove(filepath)

    # Invalidate the user's semantic cache: their previously-cached answers
    # are about different data now.
    try:
        semantic_invalidate_user_cache(user_id)
    except Exception as e:
        print(f"⚠️ Semantic cache invalidation failed: {e}")

    return {
        "message": f"File uploaded successfully! Found {len(sheet_metas)} sheet(s).",
        "file_id": file_id,
        "sheets": [m.to_dict() for m in sheet_metas],
        "user_id": user_id,
    }


# ============================================================================
# Sheet Description (User-Provided)
# ============================================================================

class DescriptionRequest(PydanticBaseModel):
    sheet_id: str
    description: str


@app.post("/describe-sheet/")
async def describe_sheet(req: DescriptionRequest):
    """Update the user-provided description for a sheet."""
    updated = update_sheet_description(req.sheet_id, req.description)
    if updated is None:
        return JSONResponse(content={"message": "Sheet not found"}, status_code=404)
    return {
        "message": "Description updated successfully",
        "sheet": updated.to_dict(),
    }


# ============================================================================
# List Sheets & Files
# ============================================================================

@app.get("/sheets/")
async def list_sheets(
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    """List all uploaded sheets with their metadata."""
    user_id = x_user_id or "anonymous"
    sheets = get_all_sheets(user_id=user_id)
    return {
        "sheets": [s.to_dict() for s in sheets],
        "count": len(sheets),
        "user_id": user_id,
    }


@app.get("/files/")
async def list_files(
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    """List all uploaded files owned by the requesting user."""
    user_id = x_user_id or "anonymous"
    files = get_all_files(user_id=user_id)
    return {
        "files": [
            {
                "file_id": f.file_id,
                "file_name": f.file_name,
                "sheet_count": f.sheet_count,
                "created_at": f.created_at,
                "last_accessed": f.last_accessed,
            }
            for f in files
        ],
        "count": len(files),
        "user_id": user_id,
    }


# ============================================================================
# Delete File
# ============================================================================

@app.delete("/files/{file_id}")
async def remove_file(
    file_id: str,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    """Delete a file and all its sheets from DB and S3."""
    user_id = x_user_id or "anonymous"
    try:
        delete_file(file_id)
        # Invalidate the user's cache: answers about the deleted file are stale.
        try:
            semantic_invalidate_user_cache(user_id)
        except Exception as e:
            print(f"⚠️ Semantic cache invalidation failed: {e}")
        return {"message": "File deleted successfully", "user_id": user_id}
    except Exception as e:
        return JSONResponse(content={"message": f"Delete failed: {e}"}, status_code=500)


# ============================================================================
# Query (Multi-Sheet)
# ============================================================================

@app.get("/query")
async def query_rag(
    query: str,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    try:
        OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
        if not OPENROUTER_API_KEY:
            return {"error": "OpenRouter API key not found"}

        user_id = x_user_id or "anonymous"
        # Used as the cache key namespace and as the model label for caching.
        # The actual model selection happens inside build_query_pipeline.
        cache_model = os.environ.get("RAG_MODEL", "openrouter/query-pipeline")

        # ------------------------------------------------------------------
        # Semantic cache check (per-user). Embed the query and look for a
        # semantically-similar cached response above the similarity threshold.
        # On hit, return the cached response without invoking the LLM pipeline.
        # ------------------------------------------------------------------
        try:
            query_embedding = embed_query(query)
            cached_response, similarity = find_similar_cached(
                user_id, query_embedding, threshold=0.92
            )
            if cached_response is not None:
                print(
                    f"⚡ Semantic cache hit for user={user_id} "
                    f"(similarity={similarity:.3f}): '{query[:60]}'"
                )
                return {
                    "answer": cached_response,
                    "cached": True,
                    "cache_type": "semantic",
                    "similarity": similarity,
                    "user_id": user_id,
                }
        except Exception as e:
            # Semantic cache failure must NEVER break a query — degrade to
            # exact-match cache + full pipeline.
            print(f"⚠️ Semantic cache lookup failed: {e}")

        client = OpenAI(
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=OPENROUTER_API_KEY
        )

        # Load all sheets from DB (scoped to the requesting user).
        all_sheet_metas = get_all_sheets(user_id=user_id)
        if not all_sheet_metas:
            return {"error": "No sheets uploaded. Please upload an Excel file first."}

        # Download each file from S3 and load sheets into DataFrames
        # Group sheets by file to avoid re-downloading
        file_cache: dict[str, str] = {}  # s3_key -> local_path
        sheets: dict[str, pd.DataFrame] = {}
        touched_file_ids: set[str] = set()

        for meta in all_sheet_metas:
            if meta.s3_key not in file_cache:
                local_path = os.path.join(UPLOAD_FOLDER, f"temp_{meta.file_id}_{meta.file_name}")
                try:
                    download_from_s3(meta.s3_key, local_path)
                    file_cache[meta.s3_key] = local_path
                except Exception as e:
                    print(f"⚠️ Could not download {meta.s3_key}: {e}")
                    continue

            filepath = file_cache[meta.s3_key]
            try:
                cleaned = ExcelService.load_all_sheets(filepath)
                if meta.sheet_name in cleaned:
                    sheets[meta.sheet_name] = cleaned[meta.sheet_name]
                    touched_file_ids.add(meta.file_id)
            except Exception as e:
                print(f"⚠️ Could not load sheet '{meta.sheet_name}': {e}")

        # Mark files whose sheets were queried as recently accessed so the
        # daily cleanup cron knows they're still in active use.
        for fid in touched_file_ids:
            try:
                touch_file_access(fid)
            except Exception as e:
                print(f"⚠️ Could not touch last_accessed for {fid}: {e}")

        # Clean up temp files
        for local_path in file_cache.values():
            if os.path.exists(local_path):
                os.remove(local_path)

        if not sheets:
            return {"error": "No valid sheets could be loaded."}

        print(f"Processing query: {query}")
        print(f"Loaded {len(sheets)} sheets: {list(sheets.keys())}")

        # Build sheet context for classification template
        sheet_context_parts = []
        for meta in all_sheet_metas:
            if meta.sheet_name in sheets:
                group_note = f" (schema group: {meta.schema_group})" if meta.schema_group and meta.schema_group != "unique" else ""
                desc_note = f"\n      Description: {meta.combined_description}" if meta.combined_description != "No description available." else ""
                sheet_context_parts.append(
                    f"  - Sheet '{meta.sheet_name}' (file: {meta.file_name}){group_note}\n"
                    f"      Fields: {', '.join(meta.fields[:20])}\n"
                    f"      Years: {', '.join(meta.years)}{desc_note}"
                )
        sheet_context = "\n".join(sheet_context_parts)

        # Format classification template with sheet context
        template = ClassTemplates.CLASSIFIER_PROMPT.format(
            sheet_context=sheet_context,
            query=query,
        )

        # Build and run pipeline
        pipeline = build_query_pipeline(
            client, sheets, all_sheet_metas, PromptTemplate(template),
            user_id=user_id,
        )
        result = await pipeline(query)

        # ------------------------------------------------------------------
        # Store the result in the semantic cache for future paraphrased hits.
        # ------------------------------------------------------------------
        try:
            response_text = (
                result.get("answer") if isinstance(result, dict) else str(result)
            )
            if response_text:
                store_cached(
                    user_id=user_id,
                    query=query,
                    query_embedding=embed_query(query),
                    response=str(response_text),
                    model=cache_model,
                )
        except Exception as e:
            print(f"⚠️ Failed to write semantic cache entry: {e}")

        # Tag the response with cache metadata so the front-end can show it.
        if isinstance(result, dict):
            result.setdefault("cached", False)
            result.setdefault("cache_type", "miss")
            result.setdefault("user_id", user_id)
        return result
    except Exception as e:
        print(f"Error in query_rag: {str(e)}")
        import traceback
        traceback.print_exc()
        return {"error": str(e)}


# ============================================================================
# Cache & Storage Management
# ============================================================================

@app.get("/cache/stats")
async def cache_stats(
    user_id: str | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    """Return LLM cache statistics, optionally scoped to a single user.

    The ``user_id`` query parameter takes precedence; otherwise we fall back
    to the ``X-User-ID`` header (same convention as the rest of the API).
    """
    effective_user = user_id or x_user_id or None
    stats = get_cache_stats(user_id=effective_user)
    stats["semantic_cache_available"] = semantic_cache_available()
    return stats


@app.post("/cache/cleanup")
async def cache_cleanup(max_age_days: int = 30):
    """Delete cache entries not accessed in the last max_age_days days."""
    deleted = cleanup_old_cache_entries(max_age_days)
    return {"message": f"Deleted {deleted} cache entries", "deleted_count": deleted}


@app.get("/storage/stats")
async def storage_stats():
    """Return storage statistics across all storage tiers."""
    files = get_all_files()
    sheets = get_all_sheets()
    cache = get_cache_stats()
    files_with_access = sum(1 for f in files if f.last_accessed)
    return {
        "s3_files": len(files),
        "total_sheets": len(sheets),
        "llm_cache": cache,
        "files_with_last_accessed": files_with_access,
        "files": [
            {
                "file_id": f.file_id,
                "file_name": f.file_name,
                "sheet_count": f.sheet_count,
                "created_at": f.created_at,
                "last_accessed": f.last_accessed,
            }
            for f in files
        ],
    }


@app.get("/storage/stale")
async def storage_stale():
    """Return files not accessed in 30/60/90 days.

    The front-end "Sheet Manager" UI uses this to surface a "Stale files"
    warning before the daily cleanup cron deletes the underlying metadata.
    """
    buckets = find_stale_files()
    return {
        "not_accessed_30d": [
            {
                "file_id": f.file_id,
                "file_name": f.file_name,
                "sheet_count": f.sheet_count,
                "created_at": f.created_at,
                "last_accessed": f.last_accessed,
            }
            for f in buckets["not_accessed_30d"]
        ],
        "not_accessed_60d": [
            {
                "file_id": f.file_id,
                "file_name": f.file_name,
                "sheet_count": f.sheet_count,
                "created_at": f.created_at,
                "last_accessed": f.last_accessed,
            }
            for f in buckets["not_accessed_60d"]
        ],
        "not_accessed_90d": [
            {
                "file_id": f.file_id,
                "file_name": f.file_name,
                "sheet_count": f.sheet_count,
                "created_at": f.created_at,
                "last_accessed": f.last_accessed,
            }
            for f in buckets["not_accessed_90d"]
        ],
        "counts": {k: len(v) for k, v in buckets.items()},
    }


@app.post("/cleanup/run")
async def cleanup_run(max_age_days: int = 90, cache_max_age_days: int = 7):
    """Trigger the daily cleanup job on demand.

    Useful for local testing and for an admin endpoint; the APScheduler at 3 AM
    is the normal path.
    """
    return daily_cleanup(max_age_days=max_age_days, cache_max_age_days=cache_max_age_days)


# ============================================================================
# Serve frontend static files (SPA)
# ============================================================================

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

if _STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        index = _STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend not built")
