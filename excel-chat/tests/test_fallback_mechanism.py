#!/usr/bin/env python3
"""
Unit tests for the LLM fallback mechanism in pipeline.py.

Tests that _run_with_fallback:
1. Retries with the fallback model when the primary returns an empty response
2. Retries the primary model a second time if the fallback also fails
3. Raises PipelineError with a user-friendly message when all attempts fail
4. Re-raises non-fallback-worthy errors immediately
5. Handles connection/timeout/rate-limit errors as fallback-worthy
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

from pipeline import (
    PipelineError,
    _is_fallback_worthy_error,
    _is_empty_response_error,
    _run_with_fallback,
    PRIMARY_MODEL,
    FALLBACK_MODEL,
)


class FakeEmptyResponseError(Exception):
    """Simulates pydantic-ai's empty model response error."""
    pass


class FakeConnectionError(Exception):
    """Simulates a connection/server error."""
    pass


class FakeValidationError(Exception):
    """Simulates a non-fallback-worthy validation error."""
    pass


def test_is_empty_response_error():
    assert _is_empty_response_error(FakeEmptyResponseError("Received empty model response"))
    assert _is_empty_response_error(FakeEmptyResponseError("empty model response"))
    assert not _is_empty_response_error(FakeValidationError("Invalid input"))


def test_is_fallback_worthy_empty_response():
    assert _is_fallback_worthy_error(FakeEmptyResponseError("Received empty model response"))


def test_is_fallback_worthy_connection_error():
    assert _is_fallback_worthy_error(FakeConnectionError("Connection timeout"))
    assert _is_fallback_worthy_error(FakeConnectionError("502 Bad Gateway"))
    assert _is_fallback_worthy_error(FakeConnectionError("rate limit exceeded"))


def test_is_fallback_worthy_validation_retries():
    assert _is_fallback_worthy_error(Exception("Exceeded maximum retries for validation"))


def test_is_fallback_worthy_non_fallback():
    assert not _is_fallback_worthy_error(FakeValidationError("Invalid input format"))
    assert not _is_fallback_worthy_error(ValueError("Something else went wrong"))


def test_fallback_succeeds_on_second_attempt():
    """Primary fails with empty response, fallback succeeds."""
    mock_result = MagicMock()
    mock_result.data = {"answer": "test"}

    primary_agent = MagicMock()
    primary_agent.run = AsyncMock(side_effect=FakeEmptyResponseError("Received empty model response"))

    fallback_agent = MagicMock()
    fallback_agent.run = AsyncMock(return_value=mock_result)

    def build_agent(*args, **kwargs):
        if "model_name" in kwargs and kwargs["model_name"] == FALLBACK_MODEL:
            return fallback_agent
        return primary_agent

    result = asyncio.run(_run_with_fallback(build_agent, "test prompt"))
    assert result == mock_result
    assert primary_agent.run.call_count == 1
    assert fallback_agent.run.call_count == 1


def test_fallback_succeeds_on_third_attempt():
    """Primary fails, fallback fails, primary retry succeeds."""
    mock_result = MagicMock()
    mock_result.data = {"answer": "test"}

    primary_agent_1 = MagicMock()
    primary_agent_1.run = AsyncMock(side_effect=FakeEmptyResponseError("Received empty model response"))

    fallback_agent = MagicMock()
    fallback_agent.run = AsyncMock(side_effect=FakeEmptyResponseError("Received empty model response"))

    primary_agent_2 = MagicMock()
    primary_agent_2.run = AsyncMock(return_value=mock_result)

    call_count = [0]

    def build_agent(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return primary_agent_1
        elif call_count[0] == 2:
            return fallback_agent
        else:
            return primary_agent_2

    result = asyncio.run(_run_with_fallback(build_agent, "test prompt"))
    assert result == mock_result
    assert primary_agent_1.run.call_count == 1
    assert fallback_agent.run.call_count == 1
    assert primary_agent_2.run.call_count == 1


def test_all_attempts_fail_raises_pipeline_error():
    """All 3 attempts fail with empty response → PipelineError with friendly message."""
    primary_agent = MagicMock()
    primary_agent.run = AsyncMock(side_effect=FakeEmptyResponseError("Received empty model response"))

    fallback_agent = MagicMock()
    fallback_agent.run = AsyncMock(side_effect=FakeEmptyResponseError("Received empty model response"))

    def build_agent(*args, **kwargs):
        if "model_name" in kwargs and kwargs["model_name"] == FALLBACK_MODEL:
            return fallback_agent
        return primary_agent

    with pytest.raises(PipelineError) as exc_info:
        asyncio.run(_run_with_fallback(build_agent, "test prompt"))

    assert "could not process this query" in str(exc_info.value).lower()
    assert "multiple attempts" in str(exc_info.value).lower()
    # Verify it was raised from the empty response error
    assert isinstance(exc_info.value.__cause__, FakeEmptyResponseError)


def test_non_fallback_error_re_raised_immediately():
    """Non-fallback-worthy errors are re-raised without retrying."""
    primary_agent = MagicMock()
    primary_agent.run = AsyncMock(side_effect=FakeValidationError("Invalid input format"))

    def build_agent(*args, **kwargs):
        return primary_agent

    with pytest.raises(FakeValidationError):
        asyncio.run(_run_with_fallback(build_agent, "test prompt"))

    # Should only have been called once (no retries)
    assert primary_agent.run.call_count == 1


def test_fallback_with_deps():
    """Fallback works correctly when deps are provided."""
    mock_result = MagicMock()
    mock_result.data = {"answer": "test"}

    deps = MagicMock()

    primary_agent = MagicMock()
    primary_agent.run = AsyncMock(side_effect=FakeEmptyResponseError("Received empty model response"))

    fallback_agent = MagicMock()
    fallback_agent.run = AsyncMock(return_value=mock_result)

    def build_agent(*args, **kwargs):
        if "model_name" in kwargs and kwargs["model_name"] == FALLBACK_MODEL:
            return fallback_agent
        return primary_agent

    result = asyncio.run(_run_with_fallback(build_agent, "test prompt", deps=deps))
    assert result == mock_result
    # Verify deps were passed to both agents
    primary_agent.run.assert_called_once_with("test prompt", deps=deps)
    fallback_agent.run.assert_called_once_with("test prompt", deps=deps)


def test_connection_error_triggers_fallback():
    """Connection errors (not just empty responses) trigger fallback."""
    mock_result = MagicMock()
    mock_result.data = {"answer": "test"}

    primary_agent = MagicMock()
    primary_agent.run = AsyncMock(side_effect=FakeConnectionError("Connection timeout"))

    fallback_agent = MagicMock()
    fallback_agent.run = AsyncMock(return_value=mock_result)

    def build_agent(*args, **kwargs):
        if "model_name" in kwargs and kwargs["model_name"] == FALLBACK_MODEL:
            return fallback_agent
        return primary_agent

    result = asyncio.run(_run_with_fallback(build_agent, "test prompt"))
    assert result == mock_result
    assert fallback_agent.run.call_count == 1
