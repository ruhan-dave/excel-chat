"""
Persistent sheet metadata store using SQLite.
Tracks uploaded Excel files, their sheets, schema groups, and descriptions.
Excel files are stored in S3; metadata is stored locally in SQLite.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import boto3
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

S3_BUCKET = os.environ.get("S3_BUCKET", "ragsheets")
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(__file__))
DB_PATH = os.path.join(DATA_DIR, "sheets.db")


# ============================================================================
# Data Models
# ============================================================================

@dataclass
class SheetMeta:
    """Metadata for a single sheet within an uploaded Excel file."""
    sheet_id: str
    file_id: str
    file_name: str
    sheet_name: str
    s3_key: str
    fields: list[str] = field(default_factory=list)
    years: list[str] = field(default_factory=list)
    schema_group: str = ""
    user_description: str = ""
    auto_description: str = ""
    row_count: int = 0

    @property
    def combined_description(self) -> str:
        parts = []
        if self.user_description:
            parts.append(f"User description: {self.user_description}")
        if self.auto_description:
            parts.append(f"Auto-generated: {self.auto_description}")
        return " | ".join(parts) if parts else "No description available."

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_id": self.sheet_id,
            "file_id": self.file_id,
            "file_name": self.file_name,
            "sheet_name": self.sheet_name,
            "s3_key": self.s3_key,
            "fields": self.fields,
            "years": self.years,
            "schema_group": self.schema_group,
            "user_description": self.user_description,
            "auto_description": self.auto_description,
            "row_count": self.row_count,
            "combined_description": self.combined_description,
        }


@dataclass
class FileMeta:
    """Metadata for an uploaded Excel file."""
    file_id: str
    file_name: str
    s3_key: str
    sheet_count: int = 0
    created_at: str = ""
    last_accessed: str | None = None


# ============================================================================
# SQLite Persistence
# ============================================================================

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize the SQLite database with required tables."""
    conn = _get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            s3_key TEXT NOT NULL,
            sheet_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            user_id TEXT DEFAULT 'anonymous'
        );

        CREATE TABLE IF NOT EXISTS sheets (
            sheet_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            file_name TEXT NOT NULL,
            sheet_name TEXT NOT NULL,
            s3_key TEXT NOT NULL,
            fields_json TEXT DEFAULT '[]',
            years_json TEXT DEFAULT '[]',
            schema_group TEXT DEFAULT '',
            user_description TEXT DEFAULT '',
            auto_description TEXT DEFAULT '',
            row_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            user_id TEXT DEFAULT 'anonymous',
            FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS llm_cache (
            cache_key TEXT PRIMARY KEY,
            response TEXT NOT NULL,
            model TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            last_accessed TEXT DEFAULT (datetime('now')),
            hit_count INTEGER DEFAULT 0,
            user_id TEXT DEFAULT 'anonymous',
            embedding_json TEXT DEFAULT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_llm_cache_last_accessed
        ON llm_cache(last_accessed);
    """)
    # Migration: add columns to pre-existing schemas. Run BEFORE the
    # user_id index is created — otherwise ``CREATE INDEX ... (user_id)``
    # fails on legacy tables that don't yet have the column.
    migrations = [
        ("files", "last_accessed", "ALTER TABLE files ADD COLUMN last_accessed TEXT DEFAULT NULL"),
        ("files", "user_id", "ALTER TABLE files ADD COLUMN user_id TEXT DEFAULT 'anonymous'"),
        ("sheets", "user_id", "ALTER TABLE sheets ADD COLUMN user_id TEXT DEFAULT 'anonymous'"),
        ("llm_cache", "user_id", "ALTER TABLE llm_cache ADD COLUMN user_id TEXT DEFAULT 'anonymous'"),
        ("llm_cache", "embedding_json", "ALTER TABLE llm_cache ADD COLUMN embedding_json TEXT DEFAULT NULL"),
    ]
    for table, col, ddl in migrations:
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if col not in existing:
            conn.execute(ddl)
    # Indexes that reference migrated columns must come AFTER the ALTERs.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_llm_cache_user_id "
        "ON llm_cache(user_id)"
    )

    # Result cache table for intermediate pipeline results
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS result_cache (
            cache_key TEXT NOT NULL,
            user_id TEXT NOT NULL,
            value TEXT NOT NULL,
            cache_type TEXT NOT NULL DEFAULT 'retrieve',
            structured_key TEXT,
            description TEXT,
            embedding_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            last_accessed TEXT DEFAULT (datetime('now')),
            hit_count INTEGER DEFAULT 0,
            PRIMARY KEY (cache_key, user_id)
        );

        CREATE INDEX IF NOT EXISTS idx_result_cache_user
        ON result_cache(user_id);

        CREATE INDEX IF NOT EXISTS idx_result_cache_user_type
        ON result_cache(user_id, cache_type);
    """)

    # Thread / message tables for conversation persistence
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            title TEXT NOT NULL DEFAULT 'New Thread',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            last_message_preview TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS thread_sheets (
            thread_id TEXT NOT NULL,
            sheet_id TEXT NOT NULL,
            added_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (thread_id, sheet_id),
            FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE,
            FOREIGN KEY (sheet_id) REFERENCES sheets(sheet_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            user_id TEXT NOT NULL DEFAULT 'anonymous',
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            query TEXT,
            friendly_response TEXT,
            full_result TEXT,
            sheet_ids TEXT,
            cached INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (thread_id) REFERENCES threads(thread_id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_threads_user
        ON threads(user_id);

        CREATE INDEX IF NOT EXISTS idx_messages_thread
        ON messages(thread_id);

        CREATE INDEX IF NOT EXISTS idx_thread_sheets_thread
        ON thread_sheets(thread_id);
    """)
    conn.commit()
    conn.close()


# ============================================================================
# S3 Helpers
# ============================================================================

def _get_s3_client():
    return boto3.client("s3")


def upload_to_s3(file_path: str, s3_key: str) -> None:
    """Upload a file to S3."""
    s3 = _get_s3_client()
    s3.upload_file(file_path, S3_BUCKET, s3_key)


def download_from_s3(s3_key: str, local_path: str) -> None:
    """Download a file from S3 to a local path."""
    s3 = _get_s3_client()
    s3.download_file(S3_BUCKET, s3_key, local_path)


def read_excel_from_s3(s3_key: str) -> dict[str, pd.DataFrame]:
    """Read an Excel file from S3 directly into pandas DataFrames without writing to disk.

    Streams the file content into a BytesIO buffer, then parses all sheets
    from that buffer. This avoids the disk write + disk read round-trip
    needed when using ``download_from_s3`` + ``pandas.read_excel(filepath)``.

    The returned dict maps sheet_name -> cleaned DataFrame. Sheets that fail
    cleaning are skipped with a warning (same behavior as
    ``ExcelService.load_all_sheets``).

    Optimization 5: avoids ~2-3s of disk I/O per file per query.
    """
    from excelservices import ExcelService

    s3 = _get_s3_client()
    response = s3.get_object(Bucket=S3_BUCKET, Key=s3_key)
    buffer = io.BytesIO(response["Body"].read())
    return ExcelService.load_all_sheets_buffer(buffer)


def delete_from_s3(s3_key: str) -> None:
    """Delete a file from S3."""
    s3 = _get_s3_client()
    s3.delete_object(Bucket=S3_BUCKET, Key=s3_key)


# ============================================================================
# CRUD Operations
# ============================================================================

def save_file(
    file_id: str, file_name: str, s3_key: str, sheet_count: int,
    user_id: str = "anonymous",
) -> None:
    """Insert or replace a file record."""
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO files (file_id, file_name, s3_key, sheet_count, user_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (file_id, file_name, s3_key, sheet_count, user_id),
    )
    conn.commit()
    conn.close()


def save_sheet(meta: SheetMeta, user_id: str | None = None) -> None:
    """Insert or replace a sheet record.

    If ``user_id`` is provided it overrides the SheetMeta value (the dataclass
    has no user_id field today, so callers pass it explicitly).
    """
    conn = _get_db()
    owner = user_id or "anonymous"
    conn.execute(
        """INSERT OR REPLACE INTO sheets
        (sheet_id, file_id, file_name, sheet_name, s3_key, fields_json, years_json,
         schema_group, user_description, auto_description, row_count, user_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            meta.sheet_id, meta.file_id, meta.file_name, meta.sheet_name,
            meta.s3_key, json.dumps(meta.fields), json.dumps(meta.years),
            meta.schema_group, meta.user_description, meta.auto_description,
            meta.row_count, owner,
        ),
    )
    conn.commit()
    conn.close()


def update_sheet_description(sheet_id: str, user_description: str) -> SheetMeta | None:
    """Update the user-provided description for a sheet."""
    conn = _get_db()
    conn.execute(
        "UPDATE sheets SET user_description = ? WHERE sheet_id = ?",
        (user_description, sheet_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
    conn.close()
    return _row_to_sheetmeta(row) if row else None


def update_sheet_auto_description(sheet_id: str, auto_description: str) -> None:
    """Update the auto-generated description for a sheet."""
    conn = _get_db()
    conn.execute(
        "UPDATE sheets SET auto_description = ? WHERE sheet_id = ?",
        (auto_description, sheet_id),
    )
    conn.commit()
    conn.close()


def update_sheet_schema_group(sheet_id: str, schema_group: str) -> None:
    """Update the schema group assignment for a sheet."""
    conn = _get_db()
    conn.execute(
        "UPDATE sheets SET schema_group = ? WHERE sheet_id = ?",
        (schema_group, sheet_id),
    )
    conn.commit()
    conn.close()


def get_all_sheets(user_id: str | None = None) -> list[SheetMeta]:
    """Get all sheet metadata, ordered by file_name then sheet_name.

    If ``user_id`` is provided, only sheets owned by that user are returned.
    """
    conn = _get_db()
    if user_id is None:
        rows = conn.execute(
            "SELECT * FROM sheets ORDER BY file_name, sheet_name"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sheets WHERE user_id = ? ORDER BY file_name, sheet_name",
            (user_id,),
        ).fetchall()
    conn.close()
    return [_row_to_sheetmeta(r) for r in rows]


def get_sheets_by_file(file_id: str) -> list[SheetMeta]:
    """Get all sheets for a given file."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM sheets WHERE file_id = ? ORDER BY sheet_name", (file_id,)
    ).fetchall()
    conn.close()
    return [_row_to_sheetmeta(r) for r in rows]


def get_sheet(sheet_id: str) -> SheetMeta | None:
    """Get a single sheet by ID."""
    conn = _get_db()
    row = conn.execute("SELECT * FROM sheets WHERE sheet_id = ?", (sheet_id,)).fetchone()
    conn.close()
    return _row_to_sheetmeta(row) if row else None


def get_all_files(user_id: str | None = None) -> list[FileMeta]:
    """Get all file records.

    If ``user_id`` is provided, only files owned by that user are returned.
    """
    conn = _get_db()
    if user_id is None:
        rows = conn.execute("SELECT * FROM files ORDER BY created_at DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM files WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    conn.close()
    return [
        FileMeta(
            file_id=r["file_id"],
            file_name=r["file_name"],
            s3_key=r["s3_key"],
            sheet_count=r["sheet_count"],
            created_at=r["created_at"] or "",
            last_accessed=r["last_accessed"],
        )
        for r in rows
    ]


def delete_file(file_id: str) -> None:
    """Delete a file and all its sheets from DB and S3."""
    conn = _get_db()
    sheets = conn.execute("SELECT * FROM sheets WHERE file_id = ?", (file_id,)).fetchall()
    for s in sheets:
        try:
            delete_from_s3(s["s3_key"])
        except Exception as e:
            print(f"⚠️ Could not delete S3 object {s['s3_key']}: {e}")
    conn.execute("DELETE FROM sheets WHERE file_id = ?", (file_id,))
    row = conn.execute("SELECT * FROM files WHERE file_id = ?", (file_id,)).fetchone()
    if row:
        try:
            delete_from_s3(row["s3_key"])
        except Exception as e:
            print(f"⚠️ Could not delete S3 object {row['s3_key']}: {e}")
    conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
    conn.commit()
    conn.close()


def touch_file_access(file_id: str) -> None:
    """Mark a file as accessed right now.

    Used to track which uploaded files are still in active use, so that the
    daily cleanup cron can decide which files are orphaned candidates for
    deletion.
    """
    conn = _get_db()
    conn.execute(
        "UPDATE files SET last_accessed = datetime('now') WHERE file_id = ?",
        (file_id,),
    )
    conn.commit()
    conn.close()


def find_old_files(max_age_days: int) -> list[FileMeta]:
    """Return files whose created_at is older than max_age_days days.

    Used by the daily cleanup cron after S3 lifecycle has expired the
    underlying objects.
    """
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM files WHERE created_at < datetime('now', ?) "
        "ORDER BY created_at ASC",
        (f"-{max_age_days} days",),
    ).fetchall()
    conn.close()
    return [
        FileMeta(
            file_id=r["file_id"],
            file_name=r["file_name"],
            s3_key=r["s3_key"],
            sheet_count=r["sheet_count"],
            created_at=r["created_at"] or "",
            last_accessed=r["last_accessed"],
        )
        for r in rows
    ]


def find_stale_files() -> dict[str, list[FileMeta]]:
    """Bucket files by how long it's been since they were last accessed.

    Returns a dict with keys ``not_accessed_30d``, ``not_accessed_60d``, and
    ``not_accessed_90d`` (each a list of FileMeta). Files with no
    ``last_accessed`` value are treated as fresh on the assumption they were
    just uploaded; only ``last_accessed`` values older than the threshold
    are reported.
    """
    conn = _get_db()
    rows = conn.execute(
        "SELECT * FROM files WHERE last_accessed IS NOT NULL "
        "ORDER BY last_accessed ASC"
    ).fetchall()
    conn.close()
    buckets: dict[str, list[FileMeta]] = {
        "not_accessed_30d": [],
        "not_accessed_60d": [],
        "not_accessed_90d": [],
    }
    threshold_30 = _sqlite_datetime_offset(30)
    threshold_60 = _sqlite_datetime_offset(60)
    threshold_90 = _sqlite_datetime_offset(90)
    for r in rows:
        meta = FileMeta(
            file_id=r["file_id"],
            file_name=r["file_name"],
            s3_key=r["s3_key"],
            sheet_count=r["sheet_count"],
            created_at=r["created_at"] or "",
            last_accessed=r["last_accessed"],
        )
        # SQLite's datetime() is comparable lexicographically as ISO-8601.
        if meta.last_accessed:
            if meta.last_accessed <= threshold_30:
                buckets["not_accessed_30d"].append(meta)
            if meta.last_accessed <= threshold_60:
                buckets["not_accessed_60d"].append(meta)
            if meta.last_accessed <= threshold_90:
                buckets["not_accessed_90d"].append(meta)
    return buckets


def _sqlite_datetime_offset(days: int) -> str:
    """Return a datetime string N days in the past (UTC, ISO-8601).

    Used only for client-side threshold comparison since SQLite returns the
    stored value as a plain string.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%d %H:%M:%S")


def _row_to_sheetmeta(row: sqlite3.Row) -> SheetMeta:
    return SheetMeta(
        sheet_id=row["sheet_id"],
        file_id=row["file_id"],
        file_name=row["file_name"],
        sheet_name=row["sheet_name"],
        s3_key=row["s3_key"],
        fields=json.loads(row["fields_json"]),
        years=json.loads(row["years_json"]),
        schema_group=row["schema_group"] or "",
        user_description=row["user_description"] or "",
        auto_description=row["auto_description"] or "",
        row_count=row["row_count"],
    )


# ============================================================================
# LLM Response Cache (exact-match)
# ============================================================================

import hashlib as _hashlib


def _cache_key(model: str, prompt: str) -> str:
    """Generate a deterministic cache key from model + prompt."""
    raw = f"{model}:{prompt}"
    return _hashlib.sha256(raw.encode()).hexdigest()


def get_cached_response(
    model: str, prompt: str, user_id: str = "anonymous"
) -> str | None:
    """Return cached LLM response for (model, prompt) if it exists, else None.

    ``user_id`` namespaces the cache so two users asking the same question
    don't share results — same question, different data, different answer.
    """
    key = _cache_key(model, prompt)
    conn = _get_db()
    row = conn.execute(
        "SELECT response FROM llm_cache WHERE cache_key = ? AND user_id = ?",
        (key, user_id),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE llm_cache SET last_accessed = datetime('now'), hit_count = hit_count + 1 "
            "WHERE cache_key = ?",
            (key,),
        )
        conn.commit()
    conn.close()
    return row["response"] if row else None


def set_cached_response(
    model: str,
    prompt: str,
    response: str,
    user_id: str = "anonymous",
    embedding: list[float] | None = None,
) -> None:
    """Store an LLM response in the cache.

    If ``embedding`` is provided (a list of floats), it's stored alongside the
    response so the semantic cache layer can do vector similarity lookups.
    """
    key = _cache_key(model, prompt)
    embedding_json = json.dumps(embedding) if embedding is not None else None
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO llm_cache "
        "(cache_key, response, model, user_id, embedding_json) VALUES (?, ?, ?, ?, ?)",
        (key, response, model, user_id, embedding_json),
    )
    conn.commit()
    conn.close()


def cleanup_old_cache_entries(max_age_days: int = 30) -> int:
    """Delete cache entries not accessed in the last max_age_days days.

    Returns the number of deleted rows.
    """
    conn = _get_db()
    cursor = conn.execute(
        "DELETE FROM llm_cache WHERE last_accessed < datetime('now', ?)",
        (f"-{max_age_days} days",),
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


def delete_files_older_than(max_age_days: int = 90) -> list[str]:
    """Delete file + sheet records older than max_age_days days.

    S3 lifecycle will already have removed the underlying objects, so this is
    purely metadata cleanup. Returns the list of deleted ``file_id`` values.
    """
    deleted_ids: list[str] = []
    for f in find_old_files(max_age_days):
        delete_file(f.file_id)
        deleted_ids.append(f.file_id)
    return deleted_ids


def get_cache_stats(user_id: str | None = None) -> dict:
    """Return summary statistics about the LLM cache.

    If ``user_id`` is provided, the stats are scoped to that user.
    """
    conn = _get_db()
    if user_id is None:
        total = conn.execute("SELECT COUNT(*) as c FROM llm_cache").fetchone()["c"]
        total_hits = conn.execute(
            "SELECT COALESCE(SUM(hit_count), 0) as h FROM llm_cache"
        ).fetchone()["h"]
    else:
        total = conn.execute(
            "SELECT COUNT(*) as c FROM llm_cache WHERE user_id = ?", (user_id,)
        ).fetchone()["c"]
        total_hits = conn.execute(
            "SELECT COALESCE(SUM(hit_count), 0) as h FROM llm_cache WHERE user_id = ?",
            (user_id,),
        ).fetchone()["h"]
    conn.close()
    return {
        "total_entries": total,
        "total_hits": total_hits,
        "user_id": user_id,
    }


def list_user_embeddings(user_id: str) -> list[tuple[str, str, list[float]]]:
    """Return ``(cache_key, response, embedding)`` tuples for a user.

    Used by the SQLite fallback in semantic_cache.find_similar_cached() to do
    cosine-similarity search in NumPy. Returns an empty list if the user has
    no entries with embeddings.
    """
    conn = _get_db()
    rows = conn.execute(
        "SELECT cache_key, response, embedding_json FROM llm_cache "
        "WHERE user_id = ? AND embedding_json IS NOT NULL",
        (user_id,),
    ).fetchall()
    conn.close()
    out: list[tuple[str, str, list[float]]] = []
    for r in rows:
        try:
            emb = json.loads(r["embedding_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not emb:
            continue
        out.append((r["cache_key"], r["response"], emb))
    return out


def invalidate_user_cache(user_id: str) -> int:
    """Delete all llm_cache rows owned by ``user_id``.

    Called when a user uploads a new file or deletes one — their cached
    answers about the old data are now stale.
    """
    conn = _get_db()
    cursor = conn.execute(
        "DELETE FROM llm_cache WHERE user_id = ?", (user_id,)
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============================================================================
# Result Cache CRUD (intermediate pipeline results)
# ============================================================================

def result_cache_get(user_id: str, cache_key: str) -> str | None:
    """Get a cached intermediate result by key + user."""
    conn = _get_db()
    row = conn.execute(
        "SELECT value FROM result_cache WHERE cache_key = ? AND user_id = ?",
        (cache_key, user_id),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    # Update hit count and last_accessed
    conn.execute(
        "UPDATE result_cache SET hit_count = hit_count + 1, "
        "last_accessed = datetime('now') "
        "WHERE cache_key = ? AND user_id = ?",
        (cache_key, user_id),
    )
    conn.commit()
    conn.close()
    return row["value"]


def result_cache_set(
    user_id: str,
    cache_key: str,
    value: str,
    cache_type: str = "retrieve",
    description: str | None = None,
    structured_key: str | None = None,
    embedding: list[float] | None = None,
) -> None:
    """Store an intermediate result in the cache."""
    import json as _json
    emb_json = _json.dumps(embedding) if embedding is not None else None
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO result_cache "
        "(cache_key, user_id, value, cache_type, structured_key, description, embedding_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (cache_key, user_id, value, cache_type, structured_key, description, emb_json),
    )
    conn.commit()
    conn.close()


def list_user_result_embeddings(
    user_id: str, cache_type: str | None = None,
) -> list[tuple[str, str, list[float], str | None]]:
    """List all result_cache entries with embeddings for a user.

    Returns list of (cache_key, value, embedding, structured_key) tuples.
    """
    import json as _json
    conn = _get_db()
    if cache_type:
        rows = conn.execute(
            "SELECT cache_key, value, embedding_json, structured_key "
            "FROM result_cache WHERE user_id = ? AND cache_type = ? "
            "AND embedding_json IS NOT NULL",
            (user_id, cache_type),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT cache_key, value, embedding_json, structured_key "
            "FROM result_cache WHERE user_id = ? AND embedding_json IS NOT NULL",
            (user_id,),
        ).fetchall()
    conn.close()

    out = []
    for r in rows:
        try:
            emb = _json.loads(r["embedding_json"])
        except (TypeError, ValueError):
            continue
        out.append((r["cache_key"], r["value"], emb, r["structured_key"]))
    return out


def invalidate_user_result_cache(user_id: str) -> int:
    """Delete all result_cache rows for a user."""
    conn = _get_db()
    cursor = conn.execute(
        "DELETE FROM result_cache WHERE user_id = ?", (user_id,)
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============================================================================
# Thread & Message CRUD (conversation persistence)
# ============================================================================

def create_thread(
    user_id: str = "anonymous",
    title: str = "New Thread",
    sheet_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Create a new thread and optionally associate sheets with it."""
    import uuid
    thread_id = str(uuid.uuid4())
    conn = _get_db()
    conn.execute(
        "INSERT INTO threads (thread_id, user_id, title) VALUES (?, ?, ?)",
        (thread_id, user_id, title),
    )
    if sheet_ids:
        for sid in sheet_ids:
            conn.execute(
                "INSERT OR IGNORE INTO thread_sheets (thread_id, sheet_id) VALUES (?, ?)",
                (thread_id, sid),
            )
    conn.commit()
    conn.close()
    return {"thread_id": thread_id, "title": title, "sheet_ids": sheet_ids or []}


def get_threads(user_id: str = "anonymous") -> list[dict[str, Any]]:
    """List all threads for a user, newest first."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT t.thread_id, t.title, t.created_at, t.updated_at, t.last_message_preview, "
        "COUNT(ts.sheet_id) as sheet_count "
        "FROM threads t LEFT JOIN thread_sheets ts ON t.thread_id = ts.thread_id "
        "WHERE t.user_id = ? GROUP BY t.thread_id ORDER BY t.updated_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_thread(thread_id: str) -> dict[str, Any] | None:
    """Get a single thread by ID."""
    conn = _get_db()
    row = conn.execute(
        "SELECT thread_id, user_id, title, created_at, updated_at, last_message_preview "
        "FROM threads WHERE thread_id = ?",
        (thread_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_thread(
    thread_id: str,
    title: str | None = None,
    add_sheet_ids: list[str] | None = None,
    remove_sheet_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Update a thread's title and/or add/remove sheets."""
    conn = _get_db()
    if title is not None:
        conn.execute(
            "UPDATE threads SET title = ?, updated_at = datetime('now') WHERE thread_id = ?",
            (title, thread_id),
        )
    if add_sheet_ids:
        for sid in add_sheet_ids:
            conn.execute(
                "INSERT OR IGNORE INTO thread_sheets (thread_id, sheet_id) VALUES (?, ?)",
                (thread_id, sid),
            )
    if remove_sheet_ids:
        for sid in remove_sheet_ids:
            conn.execute(
                "DELETE FROM thread_sheets WHERE thread_id = ? AND sheet_id = ?",
                (thread_id, sid),
            )
    conn.execute(
        "UPDATE threads SET updated_at = datetime('now') WHERE thread_id = ?",
        (thread_id,),
    )
    conn.commit()
    conn.close()
    return {"thread_id": thread_id, "title": title}


def delete_thread(thread_id: str) -> bool:
    """Delete a thread and all its messages and sheet associations."""
    conn = _get_db()
    conn.execute("DELETE FROM messages WHERE thread_id = ?", (thread_id,))
    conn.execute("DELETE FROM thread_sheets WHERE thread_id = ?", (thread_id,))
    cursor = conn.execute("DELETE FROM threads WHERE thread_id = ?", (thread_id,))
    conn.commit()
    conn.close()
    return cursor.rowcount > 0


def get_thread_sheet_ids(thread_id: str) -> list[str]:
    """Get the sheet IDs associated with a thread."""
    conn = _get_db()
    rows = conn.execute(
        "SELECT sheet_id FROM thread_sheets WHERE thread_id = ?",
        (thread_id,),
    ).fetchall()
    conn.close()
    return [r["sheet_id"] for r in rows]


def save_message(
    message_id: str,
    thread_id: str,
    user_id: str = "anonymous",
    role: str = "user",
    content: str = "",
    query: str | None = None,
    friendly_response: str | None = None,
    full_result: str | None = None,
    sheet_ids: str | None = None,
    cached: int = 0,
) -> None:
    """Save a message to the messages table."""
    conn = _get_db()
    conn.execute(
        "INSERT INTO messages "
        "(message_id, thread_id, user_id, role, content, query, "
        "friendly_response, full_result, sheet_ids, cached) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (message_id, thread_id, user_id, role, content, query,
         friendly_response, full_result, sheet_ids, cached),
    )
    conn.execute(
        "UPDATE threads SET updated_at = datetime('now'), "
        "last_message_preview = ? WHERE thread_id = ?",
        (content[:200], thread_id),
    )
    conn.commit()
    conn.close()


def get_thread_messages(thread_id: str, role: str | None = None) -> list[dict[str, Any]]:
    """Get all messages in a thread, optionally filtered by role."""
    conn = _get_db()
    if role:
        rows = conn.execute(
            "SELECT message_id, thread_id, role, content, query, "
            "friendly_response, full_result, sheet_ids, cached, created_at "
            "FROM messages WHERE thread_id = ? AND role = ? ORDER BY created_at ASC",
            (thread_id, role),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT message_id, thread_id, role, content, query, "
            "friendly_response, full_result, sheet_ids, cached, created_at "
            "FROM messages WHERE thread_id = ? ORDER BY created_at ASC",
            (thread_id,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def auto_title_thread(thread_id: str, title: str) -> None:
    """Set the thread title if it's still the default 'New Thread'."""
    conn = _get_db()
    conn.execute(
        "UPDATE threads SET title = ?, updated_at = datetime('now') "
        "WHERE thread_id = ? AND title = 'New Thread'",
        (title[:100], thread_id),
    )
    conn.commit()
    conn.close()


def find_similar_in_thread(
    thread_id: str,
    query_embedding: list[float],
    threshold: float = 0.92,
) -> dict[str, Any] | None:
    """Find a similar previous question in the same thread.

    Compares the query embedding against all user messages in the thread.
    Returns the matching message dict or None.
    """
    import numpy as np
    conn = _get_db()
    rows = conn.execute(
        "SELECT message_id, content, friendly_response, full_result "
        "FROM messages WHERE thread_id = ? AND role = 'user' "
        "ORDER BY created_at ASC",
        (thread_id,),
    ).fetchall()
    conn.close()
    if not rows:
        return None

    q = np.asarray(query_embedding, dtype=np.float64)
    q_norm = float(np.linalg.norm(q))
    if q_norm == 0.0:
        return None

    best_msg = None
    best_score = 0.0
    for r in rows:
        try:
            from semantic_cache import embed_query
            prev_emb = embed_query(r["content"])
            if prev_emb is None:
                continue
            v = prev_emb.astype(np.float64)
            v_norm = float(np.linalg.norm(v))
            if v_norm == 0.0:
                continue
            score = float(np.dot(q, v) / (q_norm * v_norm))
            if score > best_score:
                best_score = score
                best_msg = dict(r)
        except Exception:
            continue

    if best_msg is not None and best_score >= threshold:
        best_msg["similarity"] = best_score
        return best_msg
    return None


def get_sheet(sheet_id: str) -> dict[str, Any] | None:
    """Get a single sheet by ID."""
    conn = _get_db()
    row = conn.execute(
        "SELECT sheet_id, file_id, file_name, sheet_name, s3_key, "
        "fields_json, years_json, schema_group, user_description, "
        "auto_description, row_count, user_id "
        "FROM sheets WHERE sheet_id = ?",
        (sheet_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    d["fields"] = json.loads(d.pop("fields_json") or "[]")
    d["years"] = json.loads(d.pop("years_json") or "[]")
    return d
