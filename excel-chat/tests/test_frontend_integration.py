#!/usr/bin/env python3
"""
Integration tests for backend API endpoints.

Verifies that the backend is running and all key endpoints are accessible.
Requires a running backend at http://127.0.0.1:8000.
"""

import requests
import time

import pytest

BASE_URL = "http://127.0.0.1:8000"


def _wait_for_backend(timeout=30):
    for _ in range(timeout):
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


@pytest.fixture(scope="module")
def backend_ready():
    if not _wait_for_backend():
        pytest.skip("Backend not running at http://127.0.0.1:8000")
    return True


def test_backend_health(backend_ready):
    """Backend /health returns 200."""
    r = requests.get(f"{BASE_URL}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_sheets_endpoint(backend_ready):
    """GET /sheets/ returns 200 with sheet list."""
    r = requests.get(f"{BASE_URL}/sheets/", headers={"X-User-ID": "ci-test"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "sheets" in data


def test_files_endpoint(backend_ready):
    """GET /files/ returns 200 with file list."""
    r = requests.get(f"{BASE_URL}/files/", headers={"X-User-ID": "ci-test"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "files" in data


def test_threads_endpoint(backend_ready):
    """GET /threads returns 200 with thread list."""
    r = requests.get(f"{BASE_URL}/threads", headers={"X-User-ID": "ci-test"}, timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert "threads" in data


def test_cache_stats_endpoint(backend_ready):
    """GET /cache/stats returns 200."""
    r = requests.get(f"{BASE_URL}/cache/stats", headers={"X-User-ID": "ci-test"}, timeout=10)
    assert r.status_code == 200


def test_upload_endpoint_rejects_get(backend_ready):
    """GET /upload/ returns 405 (Method Not Allowed) — it's POST only."""
    r = requests.get(f"{BASE_URL}/upload/", timeout=5)
    assert r.status_code == 405
