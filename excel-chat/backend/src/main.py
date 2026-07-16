from fastapi import FastAPI, Header, UploadFile, HTTPException
from excelservices import ExcelService
# from vectordbservices import VectorDBService  # ChromaDB disabled
from queryservices import QueryService
import pandas as pd
from io import StringIO, BytesIO
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pipeline import build_query_pipeline, generate_user_friendly_response, timed
import json
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
    read_excel_from_s3,
    get_cache_stats, cleanup_old_cache_entries,
    touch_file_access, find_stale_files, delete_files_older_than,
    invalidate_user_cache as sqlite_invalidate_user_cache,
    create_thread, get_threads, get_thread, update_thread, delete_thread,
    get_thread_sheet_ids, save_message, get_thread_messages,
    auto_title_thread, find_similar_in_thread, get_sheet,
)
from semantic_cache import (
    embed_query, find_similar_cached, store_cached,
    invalidate_user_cache as semantic_invalidate_user_cache,
    is_available as semantic_cache_available,
)
from pydantic import BaseModel as PydanticBaseModel
from guardrails import (
    validate_file_upload,
    screen_query,
    inject_disclaimer,
    check_data_sensitivity,
    detect_sensitive_data_in_dataframe,
    sanitize_dataframe,
    ACCEPTED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    MAX_SPREADSHEET_ROWS,
)

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

    # --- Guardrail Layer 1: File format & size validation ---
    file_size = 0
    filepath = os.path.join(UPLOAD_FOLDER, excelFile.filename)
    with open(filepath, "wb") as f:
        while chunk := await excelFile.read(1024 * 1024):
            file_size += len(chunk)
            f.write(chunk)

    upload_check = validate_file_upload(excelFile.filename, file_size=file_size)
    if not upload_check.allowed:
        if os.path.exists(filepath):
            os.remove(filepath)
        print(f"🚫 Upload rejected: {upload_check.reason}")
        return JSONResponse(content=upload_check.reject_dict(), status_code=400)

    # Load sheets to check row count and scan for sensitive data
    try:
        from excelservices import ExcelService as _ES
        raw_sheets = _ES.load_all_sheets(filepath)
    except Exception as e:
        if os.path.exists(filepath):
            os.remove(filepath)
        return JSONResponse(content={"message": f"Failed to read file: {e}"}, status_code=400)

    # Guardrail: check row count for spreadsheets
    max_rows = max((len(df) for df in raw_sheets.values()), default=0)
    row_check = validate_file_upload(
        excelFile.filename, file_size=file_size, row_count=max_rows
    )
    if not row_check.allowed:
        if os.path.exists(filepath):
            os.remove(filepath)
        print(f"🚫 Upload rejected: {row_check.reason} (max_rows={max_rows})")
        return JSONResponse(content=row_check.reject_dict(), status_code=400)

    # --- Guardrail Layer 5: Data Sensitivity Detection ---
    all_labels: list[str] = []
    for sheet_name, df in raw_sheets.items():
        found, labels = detect_sensitive_data_in_dataframe(df)
        if found:
            all_labels.extend(labels)

    if all_labels:
        # Sensitive data detected — don't save yet. Store file temporarily
        # and return a warning so the user can choose to sanitize or cancel.
        pending_id = str(uuid.uuid4())
        pending_dir = os.path.join(UPLOAD_FOLDER, "pending")
        os.makedirs(pending_dir, exist_ok=True)
        pending_path = os.path.join(pending_dir, f"{pending_id}_{excelFile.filename}")
        os.rename(filepath, pending_path)

        unique_labels = sorted(set(all_labels))
        print(f"⚠️ Sensitive data detected in {excelFile.filename}: {unique_labels}")
        return JSONResponse(
            content={
                "sensitive_data_detected": True,
                "pending_upload_id": pending_id,
                "filename": excelFile.filename,
                "detected_types": unique_labels,
                "message": (
                    "This file appears to contain sensitive personal or financial data "
                    f"({', '.join(unique_labels)}). "
                    "You can either upload a new file without this information, "
                    "or allow the app to automatically redact the sensitive data "
                    "so your info stays safe while using the app."
                ),
            },
            status_code=200,
        )

    # No sensitive data — proceed with normal upload
    return await _finalize_upload(filepath, excelFile.filename, file_size, user_id)


@app.post("/upload/confirm")
async def confirm_upload(
    pending_upload_id: str,
    action: str,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    """Confirm or cancel a pending upload that contained sensitive data.

    Args:
        pending_upload_id: ID returned from /upload/ when sensitive data was detected.
        action: Either "sanitize" (redact sensitive data and proceed) or "cancel" (discard).
    """
    user_id = x_user_id or "anonymous"

    # Find the pending file
    pending_dir = os.path.join(UPLOAD_FOLDER, "pending")
    pattern = os.path.join(pending_dir, f"{pending_upload_id}_*")
    import glob
    matches = glob.glob(pattern)
    if not matches:
        return JSONResponse(
            content={"error": "Pending upload not found or expired."},
            status_code=404,
        )
    pending_path = matches[0]
    filename = os.path.basename(pending_path).split("_", 1)[1]

    if action == "cancel":
        os.remove(pending_path)
        return {"message": "Upload cancelled. Please upload a file without sensitive data."}

    if action != "sanitize":
        os.remove(pending_path)
        return JSONResponse(
            content={"error": f"Invalid action '{action}'. Use 'sanitize' or 'cancel'."},
            status_code=400,
        )

    # Sanitize: load sheets, redact sensitive data, re-save, then proceed
    try:
        from excelservices import ExcelService as _ES
        raw_sheets = _ES.load_all_sheets(pending_path)
    except Exception as e:
        os.remove(pending_path)
        return JSONResponse(content={"message": f"Failed to read file: {e}"}, status_code=400)

    sanitized_sheets: dict[str, pd.DataFrame] = {}
    redaction_count = 0
    for sheet_name, df in raw_sheets.items():
        before = df.astype(str).values.tolist()
        df = sanitize_dataframe(df)
        after = df.astype(str).values.tolist()
        for r_before, r_after in zip(before, after):
            for v_before, v_after in zip(r_before, r_after):
                if v_before != v_after:
                    redaction_count += 1
        sanitized_sheets[sheet_name] = df

    # Write sanitized sheets back to a new Excel file
    sanitized_path = os.path.join(UPLOAD_FOLDER, filename)
    with pd.ExcelWriter(sanitized_path, engine="openpyxl") as writer:
        for sheet_name, df in sanitized_sheets.items():
            df.to_excel(writer, sheet_name=sheet_name, index=False)

    # Clean up pending file
    os.remove(pending_path)

    file_size = os.path.getsize(sanitized_path)
    print(f"🧹 Sanitized {redaction_count} cell(s) in {filename}")
    return await _finalize_upload(sanitized_path, filename, file_size, user_id)


async def _finalize_upload(
    filepath: str, filename: str, file_size: int, user_id: str
) -> dict | JSONResponse:
    """Shared upload finalization: S3 upload, sheet metadata, DB records, cache invalidation."""
    file_id = str(uuid.uuid4())
    s3_key = f"uploads/{file_id}/{filename}"

    # Upload to S3
    try:
        upload_to_s3(filepath, s3_key)
        print(f"Uploaded to S3: {s3_key}")
    except Exception as e:
        print(f"⚠️ S3 upload failed: {e}")
        if os.path.exists(filepath):
            os.remove(filepath)
        return JSONResponse(content={"message": f"Upload failed: {e}"}, status_code=500)

    # Load all sheets and create metadata
    try:
        sheet_metas = ExcelService.load_sheet_metadata_from_file(filepath, file_id, filename, s3_key)
        print(f"Found {len(sheet_metas)} sheets in {filename}")

        # Detect schema groups
        ExcelService.detect_schema_groups(sheet_metas)
        print(f"Schema groups: {set(m.schema_group for m in sheet_metas)}")

        # Save file record
        save_file(file_id, filename, s3_key, len(sheet_metas), user_id=user_id)

        # Save each sheet metadata
        for meta in sheet_metas:
            save_sheet(meta, user_id=user_id)

        # Auto-describe all sheets via LLM
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
        if os.path.exists(filepath):
            os.remove(filepath)

    # Invalidate the user's semantic cache
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

        # --- Guardrail Layers 2-4: Screen query before processing ---
        query_check = screen_query(query, user_id=user_id)
        if not query_check.allowed:
            print(f"🚫 Query rejected: {query_check.reason}")
            return JSONResponse(content=query_check.reject_dict(), status_code=400)

        # Used as the cache key namespace and as the model label for caching.
        # The actual model selection happens inside build_query_pipeline.
        cache_model = os.environ.get("RAG_MODEL", "openrouter/query-pipeline")

        # Optimization 6: per-stage timing dict surfaced to the client.
        timings: dict[str, float] = {}

        # ------------------------------------------------------------------
        # Semantic cache check (per-user). Embed the query and look for a
        # semantically-similar cached response above the similarity threshold.
        # On hit, return the cached response without invoking the LLM pipeline.
        # ------------------------------------------------------------------
        try:
            with timed("semantic_cache_lookup", timings):
                query_embedding = embed_query(query)
                cached_response, similarity = find_similar_cached(
                    user_id, query_embedding, threshold=0.88
                )
            if cached_response is not None:
                print(
                    f"Semantic cache hit for user={user_id} "
                    f"(similarity={similarity:.3f}): '{query[:60]}'"
                )
                try:
                    cached_result = json.loads(cached_response)
                except (json.JSONDecodeError, TypeError):
                    cached_result = None
                if isinstance(cached_result, dict):
                    return {
                        "answer": cached_result.get("answer", cached_response),
                        "friendly_response": cached_result.get("friendly_response", ""),
                        "cached": True,
                        "cache_type": "semantic",
                        "similarity": similarity,
                        "user_id": user_id,
                    }
                return {
                    "answer": cached_response,
                    "friendly_response": "",
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

        # Read each file from S3 directly into memory (no local temp file).
        # Cache the parsed sheets dict per s3_key so multiple sheet_metas
        # pointing at the same file only fetch + parse once.
        # Optimization 5: avoids ~2-3s of disk I/O per file per query.
        # Optimization 6: wrapped in ``timed`` so the I/O cost is visible.
        with timed("s3_load", timings):
            file_cache: dict[str, dict[str, pd.DataFrame]] = {}  # s3_key -> sheets dict
            sheets: dict[str, pd.DataFrame] = {}
            touched_file_ids: set[str] = set()

            for meta in all_sheet_metas:
                if meta.s3_key not in file_cache:
                    try:
                        file_cache[meta.s3_key] = read_excel_from_s3(meta.s3_key)
                    except Exception as e:
                        print(f"⚠️ Could not read {meta.s3_key} from S3: {e}")
                        continue

                all_sheets = file_cache[meta.s3_key]
                if meta.sheet_name in all_sheets:
                    sheets[meta.sheet_name] = all_sheets[meta.sheet_name]
                    touched_file_ids.add(meta.file_id)

            # Mark files whose sheets were queried as recently accessed so the
            # daily cleanup cron knows they're still in active use.
            for fid in touched_file_ids:
                try:
                    touch_file_access(fid)
                except Exception as e:
                    print(f"⚠️ Could not touch last_accessed for {fid}: {e}")

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
        with timed("pipeline", timings):
            result = await pipeline(query)
        # Pipeline returns its own stage timings (planner/executor) — merge.
        if isinstance(result, dict):
            pipeline_timings = result.get("timings") or {}
            if isinstance(pipeline_timings, dict):
                timings.update(pipeline_timings)

        # ------------------------------------------------------------------
        # Store the result in the semantic cache for future paraphrased hits.
        # Optimization 6: wrap in ``timed`` so embedding cost is visible.
        # ------------------------------------------------------------------
        with timed("cache_write", timings):
            try:
                cache_payload = json.dumps(result) if isinstance(result, dict) else str(result)
                if cache_payload:
                    store_cached(
                        user_id=user_id,
                        query=query,
                        query_embedding=embed_query(query),
                        response=cache_payload,
                        model=cache_model,
                    )
            except Exception as e:
                print(f"⚠️ Failed to write semantic cache entry: {e}")

        # Tag the response with cache metadata so the front-end can show it.
        if isinstance(result, dict):
            result.setdefault("cached", False)
            result.setdefault("cache_type", "miss")
            result.setdefault("user_id", user_id)
            # Surface per-stage timings so the front-end can display them.
            total = sum(timings.values())
            print(f"query_rag total: {total:.2f}s | {timings}")
            result["timings"] = timings
        return result
    except Exception as e:
        print(f"Error in query_rag: {str(e)}")
        import traceback
        traceback.print_exc()
        error_msg = str(e)
        if "Exceeded maximum retries" in error_msg:
            error_msg = "The AI model could not process this query. Please try rephrasing your question."
        return {"error": error_msg}


# ============================================================================
# Query Streaming (SSE)
# ============================================================================

@app.get("/query/stream")
async def query_stream(
    query: str,
    thread_id: str | None = None,
    sheet_ids: str | None = None,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    """Stream query results as Server-Sent Events.

    Event types:
      - status:    {"message": "..."}
      - plan:      {"task_type": "...", "plan": {...}, "items": [...], ...}
      - pre_populated: {"values": {...}}
      - execution: {"step_results": {...}, "final_answer": ..., "explanation": "..."}
      - friendly:  {"response": "..."}
      - done:      {"timings": {...}, "total": ..., "message_id": "..."}
      - error:     {"message": "..."}
      - cached:    {"answer": ..., "friendly_response": ..., "similarity": ...}

    Optional params:
      - thread_id: If provided, Q&A is persisted to the messages table
      - sheet_ids: Comma-separated sheet IDs. If provided, only load those sheets
    """
    import asyncio

    user_id = x_user_id or "anonymous"
    event_queue: asyncio.Queue = asyncio.Queue()

    async def stream_generator():
        try:
            OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
            if not OPENROUTER_API_KEY:
                yield f"event: error\ndata: {json.dumps({'message': 'OpenRouter API key not found'})}\n\n"
                return

            # --- Guardrail Layers 2-4: Screen query before processing ---
            query_check = screen_query(query, user_id=user_id)
            if not query_check.allowed:
                print(f"🚫 Query rejected: {query_check.reason}")
                yield f"event: error\ndata: {json.dumps(query_check.reject_dict())}\n\n"
                return

            # --- Semantic cache check ---
            is_cached = False
            cached_friendly = None
            try:
                query_embedding = embed_query(query)
                cached_response, similarity = find_similar_cached(
                    user_id, query_embedding, threshold=0.88
                )
                if cached_response is not None:
                    is_cached = True
                    cached_result = json.loads(cached_response) if isinstance(cached_response, str) else None
                    if isinstance(cached_result, dict):
                        cached_friendly = cached_result.get("friendly_response", "")
                        payload = {
                            "answer": cached_result.get("answer", cached_response),
                            "friendly_response": cached_result.get("friendly_response", ""),
                            "cached": True,
                            "similarity": similarity,
                        }
                    else:
                        payload = {"answer": cached_response, "friendly_response": "", "cached": True, "similarity": similarity}

                    # Persist cached response to thread if thread_id provided
                    if thread_id:
                        try:
                            msg_id = str(uuid.uuid4())
                            save_message(
                                message_id=msg_id,
                                thread_id=thread_id,
                                user_id=user_id,
                                role="user",
                                content=query,
                                query=query,
                                friendly_response=cached_friendly or "",
                                full_result=cached_response if isinstance(cached_response, str) else json.dumps(cached_response),
                                sheet_ids=sheet_ids,
                                cached=1,
                            )
                            auto_title_thread(thread_id, query)
                        except Exception as e:
                            print(f"⚠️ Failed to save cached message to thread: {e}")

                    yield f"event: cached\ndata: {json.dumps(payload)}\n\n"
                    return
            except Exception as e:
                print(f"⚠️ Semantic cache lookup failed (stream): {e}")

            # --- Load sheets from S3 ---
            # If sheet_ids provided, filter to only those sheets
            selected_sheet_ids_set = set(sheet_ids.split(",")) if sheet_ids else None
            all_sheet_metas = get_all_sheets(user_id=user_id)
            if selected_sheet_ids_set:
                all_sheet_metas = [m for m in all_sheet_metas if m.sheet_id in selected_sheet_ids_set]
            if not all_sheet_metas:
                yield f"event: error\ndata: {json.dumps({'message': 'No sheets uploaded. Please upload an Excel file first.'})}\n\n"
                return

            yield f"event: status\ndata: {json.dumps({'message': 'Loading sheets from storage…'})}\n\n"

            file_cache: dict[str, dict[str, pd.DataFrame]] = {}
            sheets: dict[str, pd.DataFrame] = {}
            touched_file_ids: set[str] = set()

            for meta in all_sheet_metas:
                if meta.s3_key not in file_cache:
                    try:
                        file_cache[meta.s3_key] = read_excel_from_s3(meta.s3_key)
                    except Exception as e:
                        print(f"⚠️ Could not read {meta.s3_key} from S3: {e}")
                        continue
                all_sheets = file_cache[meta.s3_key]
                if meta.sheet_name in all_sheets:
                    sheets[meta.sheet_name] = all_sheets[meta.sheet_name]
                    touched_file_ids.add(meta.file_id)

            for fid in touched_file_ids:
                try:
                    touch_file_access(fid)
                except Exception:
                    pass

            if not sheets:
                yield f"event: error\ndata: {json.dumps({'message': 'No valid sheets could be loaded.'})}\n\n"
                return

            # --- Build and run pipeline with streaming callback ---
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

            template = ClassTemplates.CLASSIFIER_PROMPT.format(
                sheet_context=sheet_context,
                query=query,
            )

            # The callback pushes events into the asyncio queue.
            # Since the pipeline runs as a coroutine in the same event loop,
            # we can use put_nowait — no thread-safety concerns.
            def on_event(event_type: str, data: dict):
                payload = json.dumps(data, default=str)
                event_queue.put_nowait((event_type, payload))

            pipeline = build_query_pipeline(
                None, sheets, all_sheet_metas, PromptTemplate(template),
                user_id=user_id, on_event=on_event,
            )

            # Run pipeline in background, consume events from queue concurrently
            pipeline_task = asyncio.create_task(pipeline(query))

            while True:
                # Check if pipeline is done and queue is empty
                if pipeline_task.done() and event_queue.empty():
                    break

                try:
                    event_type, payload = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event_type}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    continue

            # Drain any remaining events
            while not event_queue.empty():
                event_type, payload = await event_queue.get()
                yield f"event: {event_type}\ndata: {payload}\n\n"

            # Get the result and write to cache
            result = await pipeline_task

            cache_model = os.environ.get("RAG_MODEL", "openrouter/query-pipeline")
            try:
                cache_payload = json.dumps(result, default=str) if isinstance(result, dict) else str(result)
                if cache_payload:
                    store_cached(
                        user_id=user_id,
                        query=query,
                        query_embedding=embed_query(query),
                        response=cache_payload,
                        model=cache_model,
                    )
            except Exception as e:
                print(f"⚠️ Failed to write semantic cache entry (stream): {e}")

            # --- Persist to thread if thread_id provided ---
            message_id = None
            if thread_id:
                try:
                    message_id = str(uuid.uuid4())
                    friendly_text = result.get("friendly_response", "") if isinstance(result, dict) else ""
                    save_message(
                        message_id=message_id,
                        thread_id=thread_id,
                        user_id=user_id,
                        role="user",
                        content=query,
                        query=query,
                        friendly_response=friendly_text,
                        full_result=json.dumps(result, default=str) if isinstance(result, dict) else str(result),
                        sheet_ids=sheet_ids,
                        cached=1 if is_cached else 0,
                    )
                    auto_title_thread(thread_id, query)
                except Exception as e:
                    print(f"⚠️ Failed to save message to thread: {e}")

            # Emit done event with message_id if persisted
            done_payload = {"timings": result.get("timings", {}) if isinstance(result, dict) else {}, "total": result.get("total_time", 0) if isinstance(result, dict) else 0}
            if message_id:
                done_payload["message_id"] = message_id
            yield f"event: done\ndata: {json.dumps(done_payload)}\n\n"

        except Exception as e:
            print(f"Error in query_stream: {str(e)}")
            import traceback
            traceback.print_exc()
            error_msg = str(e)
            if "Exceeded maximum retries" in error_msg:
                error_msg = "The AI model could not process this query. Please try rephrasing your question."
            yield f"event: error\ndata: {json.dumps({'message': error_msg})}\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ============================================================================
# Thread & Message Management
# ============================================================================

class CreateThreadRequest(PydanticBaseModel):
    title: str = "New Thread"
    sheet_ids: list[str] = []

class UpdateThreadRequest(PydanticBaseModel):
    title: str | None = None
    add_sheet_ids: list[str] | None = None
    remove_sheet_ids: list[str] | None = None


@app.post("/threads")
async def create_thread_endpoint(
    req: CreateThreadRequest,
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    user_id = x_user_id or "anonymous"
    result = create_thread(user_id=user_id, title=req.title, sheet_ids=req.sheet_ids)
    return result


@app.get("/threads")
async def list_threads_endpoint(
    x_user_id: str | None = Header(default=None, alias="X-User-ID"),
):
    user_id = x_user_id or "anonymous"
    threads = get_threads(user_id=user_id)
    return {"threads": threads}


@app.get("/threads/{thread_id}")
async def get_thread_endpoint(thread_id: str):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = get_thread_messages(thread_id)
    sheet_ids = get_thread_sheet_ids(thread_id)
    sheets = []
    for sid in sheet_ids:
        s = get_sheet(sid)
        if s:
            sheets.append(s)
    return {"thread": thread, "messages": messages, "sheets": sheets}


@app.patch("/threads/{thread_id}")
async def update_thread_endpoint(thread_id: str, req: UpdateThreadRequest):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    update_thread(
        thread_id,
        title=req.title,
        add_sheet_ids=req.add_sheet_ids,
        remove_sheet_ids=req.remove_sheet_ids,
    )
    updated = get_thread(thread_id)
    return {"thread": updated}


@app.delete("/threads/{thread_id}")
async def delete_thread_endpoint(thread_id: str):
    deleted = delete_thread(thread_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Thread not found")
    return {"message": "Thread deleted", "thread_id": thread_id}


@app.get("/threads/{thread_id}/messages")
async def get_thread_messages_endpoint(thread_id: str):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    messages = get_thread_messages(thread_id)
    return {"messages": messages}


@app.get("/threads/{thread_id}/similar")
async def find_similar_in_thread_endpoint(
    thread_id: str,
    query: str,
):
    thread = get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    try:
        query_embedding = embed_query(query)
        if query_embedding is None:
            return {"found": False, "reason": "embedding model unavailable"}
        emb_list = query_embedding.tolist()
        match = find_similar_in_thread(thread_id, emb_list)
        if match:
            return {
                "found": True,
                "message_id": match["message_id"],
                "similarity": match.get("similarity", 0.0),
                "friendly_response": match.get("friendly_response", ""),
            }
        return {"found": False}
    except Exception as e:
        return {"found": False, "error": str(e)}


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

    redis_info: dict = {"connected": False}
    try:
        from cache_service import _get_redis
        r = _get_redis()
        if r is not None:
            r.ping()
            redis_info["connected"] = True
            redis_url = os.environ.get("REDIS_URL", "")
            redis_info["url"] = redis_url.split("@")[-1] if "@" in redis_url else "configured"
            try:
                info = r.info("memory")
                redis_info["used_memory_human"] = info.get("used_memory_human", "?")
                redis_info["used_memory_peak_human"] = info.get("used_memory_peak_human", "?")
            except Exception:
                pass
            try:
                redis_info["total_keys"] = r.dbsize()
            except Exception:
                pass
            try:
                namespaces = {}
                for prefix in ["llm:", "semantic:", "result:", "sandbox:"]:
                    count = 0
                    for _ in r.scan_iter(match=f"{prefix}*", count=100):
                        count += 1
                    namespaces[prefix.rstrip(":")] = count
                redis_info["keys_by_namespace"] = namespaces
            except Exception:
                pass
    except Exception as e:
        redis_info["error"] = str(e)
    stats["redis"] = redis_info

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
    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        file_path = _STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        index = _STATIC_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="Frontend not built")
