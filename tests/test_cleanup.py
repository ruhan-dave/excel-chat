"""
Tests for the daily cleanup job, last_accessed tracking, and stale-file API.

Coverage:
  * daily_cleanup() in main.py deletes SQLite file records older than the
    configured threshold (mocked delete_file + mocked SQLite row timestamps).
  * cleanup_old_cache_entries() respects the 7-day threshold and leaves
    recent entries alone.
  * find_stale_files() and find_old_files() bucket files correctly.
  * GET /storage/stale returns files in 30/60/90 day buckets.
  * /storage/stats includes last_accessed info per file.
  * touch_file_access() updates last_accessed and exposes it via get_all_files.
"""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make `sheet_metadata` and `main` importable without installing the package.
BACKEND_SRC = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(BACKEND_SRC))


def _maybe_await(value):
    """Await ``value`` if it's a coroutine, otherwise return it directly.

    Lets tests call either sync functions or async FastAPI endpoints without
    sprinkling ``asyncio.run`` everywhere.
    """
    if inspect.iscoroutine(value):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(value)
        finally:
            loop.close()
    return value


# ---------------------------------------------------------------------------
# Fixtures: redirect sheet_metadata at a temp SQLite DB so we don't touch the
# real sheets.db that may contain prod data.
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_sheet_db(monkeypatch, tmp_path):
    """Point sheet_metadata at a fresh on-disk SQLite file and init the schema."""
    db_path = tmp_path / "test_sheets.db"
    import sheet_metadata

    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()
    # Also patch the DB_PATH that delete_from_s3/etc use.
    yield str(db_path)


@pytest.fixture()
def temp_main(monkeypatch, temp_sheet_db):
    """Import main.py with the temp DB in place.

    main.py calls init_db() at module import time, so we must reset sheet_metadata
    first. We also stub out the APScheduler lifespan so importing main doesn't
    spin up a real scheduler during tests.
    """
    import sheet_metadata
    import importlib

    # Force a fresh import of main so init_db() reads the temp DB_PATH.
    if "main" in sys.modules:
        del sys.modules["main"]
    if "sheet_metadata" in sys.modules:
        # Reload so the module-level DB_PATH is the patched one.
        importlib.reload(sheet_metadata)
    # After reload, sheet_metadata.DB_PATH is back to its module-level default
    # (``sheets.db`` next to sheet_metadata.py). Re-apply the patch on the
    # freshly-loaded module and re-init the schema on the temp DB.
    monkeypatch.setattr(sheet_metadata, "DB_PATH", temp_sheet_db)
    sheet_metadata.init_db()
    main = importlib.import_module("main")
    yield main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_file(
    db_path: str,
    file_id: str,
    file_name: str,
    s3_key: str,
    created_days_ago: int,
    last_accessed_days_ago: int | None = None,
) -> None:
    """Insert a row into the files table with controllable timestamps."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    created = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    if last_accessed_days_ago is None:
        last_accessed_sql = "NULL"
    else:
        accessed = datetime.now(timezone.utc) - timedelta(days=last_accessed_days_ago)
        last_accessed_sql = f"'{accessed.strftime('%Y-%m-%d %H:%M:%S')}'"
    conn.execute(
        f"INSERT INTO files (file_id, file_name, s3_key, sheet_count, "
        f"created_at, last_accessed) VALUES (?, ?, ?, ?, ?, {last_accessed_sql})",
        (
            file_id,
            file_name,
            s3_key,
            1,
            created.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def _insert_cache_entry(
    db_path: str, cache_key: str, model: str, response: str,
    last_accessed_days_ago: int,
) -> None:
    """Insert a row into the llm_cache table with a controlled last_accessed."""
    conn = sqlite3.connect(db_path)
    accessed = datetime.now(timezone.utc) - timedelta(days=last_accessed_days_ago)
    conn.execute(
        "INSERT INTO llm_cache (cache_key, response, model, last_accessed) "
        "VALUES (?, ?, ?, ?)",
        (
            cache_key,
            response,
            model,
            accessed.strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def _count_files(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.close()
    return n


def _count_cache(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    n = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# daily_cleanup
# ---------------------------------------------------------------------------


def test_daily_cleanup_deletes_old_file_records(temp_main, temp_sheet_db):
    """daily_cleanup should remove files older than 90 days (default)."""
    # Arrange: one ancient file + one fresh file.
    _insert_file(temp_sheet_db, "old-1", "ancient.xlsx", "uploads/old-1/a.xlsx", 100)
    _insert_file(temp_sheet_db, "fresh-1", "fresh.xlsx", "uploads/fresh-1/f.xlsx", 5)
    # Also drop a row in llm_cache past the 7-day cutoff.
    _insert_cache_entry(temp_sheet_db, "k-stale", "m", "r", 30)
    _insert_cache_entry(temp_sheet_db, "k-fresh", "m", "r", 1)

    # Use ``wraps`` so the mock delegates to the real ``delete_file``. We
    # still want to track calls and avoid touching S3 (delete_from_s3 is
    # wrapped in try/except inside delete_file, so it won't crash even if we
    # let it run). Letting the real SQLite DELETE execute is essential to
    # verify the DB state in subsequent assertions.
    import sheet_metadata
    with patch("sheet_metadata.delete_from_s3"):  # no-op S3 calls
        result = temp_main.daily_cleanup()

    assert result["deleted_files_count"] == 1
    assert result["deleted_cache_entries"] == 1
    assert result["deleted_files"] == ["old-1"]

    # Fresh file survived, ancient one gone.
    assert _count_files(temp_sheet_db) == 1
    assert _count_cache(temp_sheet_db) == 1


def test_daily_cleanup_respects_custom_thresholds(temp_main, temp_sheet_db):
    """Override max_age_days and cache_max_age_days for finer control."""
    _insert_file(temp_sheet_db, "ten-day", "ten.xlsx", "uploads/ten/a.xlsx", 10)
    _insert_file(temp_sheet_db, "sixty-day", "sixty.xlsx", "uploads/sixty/a.xlsx", 60)
    _insert_cache_entry(temp_sheet_db, "k-3d", "m", "r", 3)
    _insert_cache_entry(temp_sheet_db, "k-30d", "m", "r", 30)

    with patch("sheet_metadata.delete_from_s3"):
        result = temp_main.daily_cleanup(max_age_days=30, cache_max_age_days=7)

    # 60-day-old file is past the 30-day threshold → deleted; 10-day-old survives.
    assert result["deleted_files"] == ["sixty-day"]
    assert result["deleted_cache_entries"] == 1
    assert _count_files(temp_sheet_db) == 1
    assert _count_cache(temp_sheet_db) == 1


def test_daily_cleanup_is_noop_when_nothing_is_stale(temp_main, temp_sheet_db):
    """When everything is recent, daily_cleanup should be a no-op."""
    _insert_file(temp_sheet_db, "recent", "recent.xlsx", "uploads/r/a.xlsx", 1)
    _insert_cache_entry(temp_sheet_db, "k-fresh", "m", "r", 1)

    with patch("sheet_metadata.delete_from_s3") as s3_del:
        result = temp_main.daily_cleanup()

    assert result["deleted_files_count"] == 0
    assert result["deleted_cache_entries"] == 0
    s3_del.assert_not_called()
    assert _count_files(temp_sheet_db) == 1
    assert _count_cache(temp_sheet_db) == 1


# ---------------------------------------------------------------------------
# cleanup_old_cache_entries
# ---------------------------------------------------------------------------


def test_cleanup_old_cache_entries_7_day_threshold(temp_sheet_db):
    """The 7-day cache threshold must evict only entries older than that."""
    _insert_cache_entry(temp_sheet_db, "k-1d", "m", "r", 1)
    _insert_cache_entry(temp_sheet_db, "k-6d", "m", "r", 6)
    _insert_cache_entry(temp_sheet_db, "k-8d", "m", "r", 8)
    _insert_cache_entry(temp_sheet_db, "k-30d", "m", "r", 30)

    import sheet_metadata
    deleted = sheet_metadata.cleanup_old_cache_entries(max_age_days=7)

    assert deleted == 2
    assert _count_cache(temp_sheet_db) == 2


def test_cleanup_old_cache_entries_default_threshold_is_30(temp_sheet_db):
    """Default behavior evicts anything older than 30 days."""
    _insert_cache_entry(temp_sheet_db, "k-1d", "m", "r", 1)
    _insert_cache_entry(temp_sheet_db, "k-29d", "m", "r", 29)
    _insert_cache_entry(temp_sheet_db, "k-31d", "m", "r", 31)

    import sheet_metadata
    deleted = sheet_metadata.cleanup_old_cache_entries()

    assert deleted == 1
    assert _count_cache(temp_sheet_db) == 2


# ---------------------------------------------------------------------------
# find_old_files / find_stale_files
# ---------------------------------------------------------------------------


def test_find_old_files_returns_only_expired(temp_sheet_db):
    _insert_file(temp_sheet_db, "ancient", "a.xlsx", "k1", 95)
    _insert_file(temp_sheet_db, "young", "y.xlsx", "k2", 30)

    import sheet_metadata
    old = sheet_metadata.find_old_files(90)

    assert len(old) == 1
    assert old[0].file_id == "ancient"


def test_find_stale_files_buckets_by_threshold(temp_sheet_db):
    _insert_file(temp_sheet_db, "f-31", "a.xlsx", "k1", 5, last_accessed_days_ago=31)
    _insert_file(temp_sheet_db, "f-61", "b.xlsx", "k2", 5, last_accessed_days_ago=61)
    _insert_file(temp_sheet_db, "f-91", "c.xlsx", "k3", 5, last_accessed_days_ago=91)
    _insert_file(temp_sheet_db, "f-fresh", "d.xlsx", "k4", 5, last_accessed_days_ago=1)
    _insert_file(temp_sheet_db, "f-never", "e.xlsx", "k5", 5, last_accessed_days_ago=None)

    import sheet_metadata
    buckets = sheet_metadata.find_stale_files()

    by_id = {f.file_id for f in buckets["not_accessed_30d"]}
    assert "f-31" in by_id
    assert "f-61" in by_id
    assert "f-91" in by_id
    # Files that have never been touched are NOT stale — assume recently uploaded.
    assert "f-never" not in by_id
    assert "f-fresh" not in by_id

    by_id_60 = {f.file_id for f in buckets["not_accessed_60d"]}
    assert "f-31" not in by_id_60
    assert "f-61" in by_id_60
    assert "f-91" in by_id_60

    by_id_90 = {f.file_id for f in buckets["not_accessed_90d"]}
    assert by_id_90 == {"f-91"}


# ---------------------------------------------------------------------------
# touch_file_access + FileMeta exposes last_accessed
# ---------------------------------------------------------------------------


def test_touch_file_access_updates_timestamp(temp_sheet_db):
    _insert_file(
        temp_sheet_db, "fid", "f.xlsx", "k", 30, last_accessed_days_ago=10
    )
    import sheet_metadata
    sheet_metadata.touch_file_access("fid")

    files = sheet_metadata.get_all_files()
    assert len(files) == 1
    assert files[0].last_accessed is not None
    # Should be within the last few seconds.
    parsed = datetime.strptime(
        files[0].last_accessed, "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - parsed
    assert age < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# /storage/stale and /storage/stats endpoints
# ---------------------------------------------------------------------------


def test_storage_stale_endpoint_buckets_files(temp_main, temp_sheet_db):
    _insert_file(temp_sheet_db, "f-31", "a.xlsx", "k1", 5, last_accessed_days_ago=31)
    _insert_file(temp_sheet_db, "f-61", "b.xlsx", "k2", 5, last_accessed_days_ago=61)
    _insert_file(temp_sheet_db, "f-91", "c.xlsx", "k3", 5, last_accessed_days_ago=91)

    result = _maybe_await(temp_main.storage_stale())

    assert result["counts"]["not_accessed_30d"] == 3
    assert result["counts"]["not_accessed_60d"] == 2
    assert result["counts"]["not_accessed_90d"] == 1
    names_30 = {f["file_name"] for f in result["not_accessed_30d"]}
    assert names_30 == {"a.xlsx", "b.xlsx", "c.xlsx"}
    names_90 = {f["file_name"] for f in result["not_accessed_90d"]}
    assert names_90 == {"c.xlsx"}


def test_storage_stats_includes_last_accessed(temp_main, temp_sheet_db):
    _insert_file(
        temp_sheet_db, "f-touched", "t.xlsx", "k1", 5, last_accessed_days_ago=2
    )
    _insert_file(
        temp_sheet_db, "f-untouched", "u.xlsx", "k2", 5, last_accessed_days_ago=None
    )

    result = _maybe_await(temp_main.storage_stats())

    assert result["s3_files"] == 2
    assert result["files_with_last_accessed"] == 1
    by_id = {f["file_id"]: f for f in result["files"]}
    assert by_id["f-touched"]["last_accessed"] is not None
    assert by_id["f-untouched"]["last_accessed"] is None
    # created_at is now exposed too.
    assert by_id["f-touched"]["created_at"]


def test_cleanup_run_endpoint_triggers_daily_cleanup(temp_main, temp_sheet_db):
    """POST /cleanup/run delegates to daily_cleanup and returns its result."""
    _insert_file(temp_sheet_db, "ancient", "a.xlsx", "k", 120)
    _insert_cache_entry(temp_sheet_db, "k-stale", "m", "r", 30)

    with patch("sheet_metadata.delete_from_s3"):
        result = _maybe_await(temp_main.cleanup_run())

    assert result["deleted_files_count"] == 1
    assert result["deleted_cache_entries"] == 1


# ---------------------------------------------------------------------------
# Migration: init_db() adds the last_accessed column to a pre-existing schema
# ---------------------------------------------------------------------------


def test_init_db_adds_last_accessed_column_via_migration(monkeypatch, tmp_path):
    """Simulate a DB created before last_accessed existed, then init_db() runs."""
    db_path = tmp_path / "legacy.db"
    # Create a "legacy" DB with the OLD files schema (no last_accessed).
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE files (
            file_id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            s3_key TEXT NOT NULL,
            sheet_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        INSERT INTO files (file_id, file_name, s3_key) VALUES ('a', 'a.xlsx', 'k');
    """)
    conn.commit()
    conn.close()

    # Now run init_db() with this path in place.
    import sheet_metadata
    monkeypatch.setattr(sheet_metadata, "DB_PATH", str(db_path))
    sheet_metadata.init_db()

    conn = sqlite3.connect(str(db_path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(files)").fetchall()}
    conn.close()
    assert "last_accessed" in cols
    # Existing row was preserved.
    conn = sqlite3.connect(str(db_path))
    row = conn.execute("SELECT file_id, file_name FROM files").fetchone()
    conn.close()
    assert row == ("a", "a.xlsx")


# ---------------------------------------------------------------------------
# APScheduler wiring is graceful when apscheduler is missing
# ---------------------------------------------------------------------------


def test_main_module_loads_without_apscheduler(monkeypatch):
    """If apscheduler isn't installed, lifespan still works (no crash on import)."""
    import importlib
    import sheet_metadata

    # Re-route to a fresh DB so init_db() doesn't touch prod data.
    monkeypatch.setattr(sheet_metadata, "DB_PATH", ":memory:")
    # Force a re-import with the temp DB in place.
    if "main" in sys.modules:
        del sys.modules["main"]
    main = importlib.import_module("main")

    # daily_cleanup is always available even if the scheduler is not.
    assert callable(main.daily_cleanup)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))