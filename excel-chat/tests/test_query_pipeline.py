#!/usr/bin/env python3
"""
Integration tests for the query pipeline against a running backend.

Requires:
  - Backend running at http://127.0.0.1:8000
  - OPENROUTER_API_KEY set (for real LLM calls)
  - Example Excel file at example_sheets/Detailed_Expense_Breakdown.xlsx
"""

import os
import requests
import json
import time
from pathlib import Path

import pytest

# Set fake AWS credentials BEFORE importing anything that uses boto3.
# In CI, moto intercepts boto3 calls; locally, real AWS creds are used.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

BASE_URL = "http://127.0.0.1:8000"
EXCEL_FILE = Path(__file__).parent.parent / "example_sheets" / "Detailed_Expense_Breakdown.xlsx"


def _wait_for_backend(timeout=30):
    """Poll /health until the backend is ready."""
    for _ in range(timeout):
        try:
            r = requests.get(f"{BASE_URL}/health", timeout=2)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def _upload_excel(user_id="ci-test"):
    """Upload the example Excel file and return the response."""
    with open(EXCEL_FILE, "rb") as f:
        r = requests.post(
            f"{BASE_URL}/upload/",
            files={"excelFile": (EXCEL_FILE.name, f, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
            headers={"X-User-ID": user_id},
            timeout=60,
        )
    return r


@pytest.fixture(scope="module")
def backend_ready():
    """Ensure backend is running and responsive."""
    if not _wait_for_backend():
        pytest.skip("Backend not running at http://127.0.0.1:8000")
    return True


@pytest.fixture(scope="module")
def uploaded_data(backend_ready):
    """Upload the example Excel file once for all tests in this module."""
    if not EXCEL_FILE.exists():
        pytest.skip(f"Example file not found: {EXCEL_FILE}")
    r = _upload_excel()
    assert r.status_code == 200, f"Upload failed: {r.status_code} {r.text}"
    data = r.json()

    # If sensitive data is detected, confirm with sanitize action
    if data.get("sensitive_data_detected"):
        pending_id = data["pending_upload_id"]
        r2 = requests.post(
            f"{BASE_URL}/upload/confirm",
            params={"pending_upload_id": pending_id, "action": "sanitize"},
            headers={"X-User-ID": "ci-test"},
            timeout=60,
        )
        assert r2.status_code == 200, f"Confirm failed: {r2.status_code} {r2.text}"
        data = r2.json()

    assert "file_id" in data, f"Unexpected upload response: {data}"
    return data


def test_health_endpoint(backend_ready):
    """Backend /health returns 200 with status ok."""
    r = requests.get(f"{BASE_URL}/health", timeout=5)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_upload_succeeds(uploaded_data):
    """Uploading the example Excel file returns sheet metadata."""
    assert "sheets" in uploaded_data
    assert len(uploaded_data["sheets"]) > 0


def test_query_wages_and_salaries(uploaded_data):
    """Query for wages and salaries returns a non-empty answer."""
    r = requests.get(
        f"{BASE_URL}/query",
        params={"query": "How much did I spend on wages and salaries in 2022?"},
        headers={"X-User-ID": "ci-test"},
        timeout=120,
    )
    assert r.status_code == 200, f"Query failed: {r.status_code} {r.text}"
    result = r.json()
    assert "answer" in result or "friendly" in result or "result" in result, f"Unexpected response: {result}"


def test_query_percentage_calculation(uploaded_data):
    """Query for percentage of public grants returns a non-empty answer."""
    r = requests.get(
        f"{BASE_URL}/query",
        params={"query": "What percentage of grants are from public sources?"},
        headers={"X-User-ID": "ci-test"},
        timeout=120,
    )
    assert r.status_code == 200, f"Query failed: {r.status_code} {r.text}"
    result = r.json()
    assert "answer" in result or "friendly" in result or "result" in result, f"Unexpected response: {result}"


def test_query_stream_endpoint(uploaded_data):
    """SSE streaming endpoint returns a stream of events."""
    r = requests.get(
        f"{BASE_URL}/query/stream",
        params={"query": "What were the wages and salaries in 2022?", "thread_id": ""},
        headers={"X-User-ID": "ci-test"},
        stream=True,
        timeout=120,
    )
    assert r.status_code == 200
    # Read first few chunks to confirm SSE stream is active
    chunks = []
    for line in r.iter_lines(decode_unicode=True):
        if line:
            chunks.append(line)
        if len(chunks) >= 5:
            break
    assert len(chunks) > 0, "No SSE events received"
