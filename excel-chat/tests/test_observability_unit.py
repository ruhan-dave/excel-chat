"""Offline unit tests for the Langfuse observability layer (observability.py).

No network, no Langfuse keys required.  The autouse conftest fixture strips
LANGFUSE_* env vars so tests run in disabled mode by default; tests that
exercise the enabled path set dummy keys via ``monkeypatch.setenv`` and mock
``get_langfuse()`` to return a fake client.
"""

from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

import pytest

# ---------------------------------------------------------------------------
# sys.path setup — same pattern as existing tests
# ---------------------------------------------------------------------------
backend_src = Path(__file__).parent.parent / "backend" / "src"
sys.path.insert(0, str(backend_src))

import observability
from observability import (
    is_observability_enabled,
    init_observability,
    observe_agent_run,
    observe_step,
    mask_pii,
    flush_observability,
    get_langfuse,
    _SpanProxy,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeSpan:
    """Minimal stand-in for a Langfuse span/observation."""

    def __init__(self):
        self.updates = []

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeLangfuseClient:
    """Fake client whose ``start_as_current_observation`` returns a FakeSpan.

    Captures the kwargs passed to ``start_as_current_observation`` (e.g.
    ``input``, ``name``, ``metadata``) so tests can assert on them.
    """

    def __init__(self):
        self.span = FakeSpan()
        self.observation_kwargs = {}

    def start_as_current_observation(self, **kwargs):
        self.observation_kwargs = kwargs
        return self.span

    def flush(self):
        pass


class FakeUsage:
    """Stand-in for pydantic-ai ``RunUsage``."""

    def __init__(self, input_tokens=0, output_tokens=0, cost=None):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cost = cost


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_module():
    """Reset observability module state between tests."""
    observability._initialized = False
    observability._client = None
    observability._disabled_reason = None


def _last_metadata(span: FakeSpan) -> dict:
    """Return the metadata dict from the most recent span.update call."""
    for upd in reversed(span.updates):
        if "metadata" in upd:
            return upd["metadata"]
    return {}


@pytest.fixture(autouse=True)
def _reset_observability():
    """Ensure each test starts with a clean observability module state."""
    _reset_module()
    yield
    _reset_module()


# ---------------------------------------------------------------------------
# Tests: disabled / enabled detection
# ---------------------------------------------------------------------------

def test_disabled_when_keys_missing():
    """With env stripped, observability is fully disabled (no-op, no raise)."""
    assert is_observability_enabled() is False

    with observe_agent_run("test-agent") as proxy:
        assert proxy is None

    with observe_step("test-step", input="hello") as proxy:
        assert proxy is None

    # flush must not raise when disabled
    flush_observability()


def test_enabled_when_keys_present(monkeypatch):
    """With dummy keys set, observability reports enabled."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert is_observability_enabled() is True


# ---------------------------------------------------------------------------
# Tests: PII masking
# ---------------------------------------------------------------------------

def test_mask_pii(monkeypatch):
    """Emails and phones are redacted in production environment."""
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "production")

    text = "Contact me at john@example.com or call 555-123-4567."
    masked = mask_pii(text)
    assert "[EMAIL]" in masked
    assert "[PHONE]" in masked
    assert "john@example.com" not in masked
    assert "555-123-4567" not in masked

    # Plain text without PII is unchanged
    plain = "There is no PII here."
    assert mask_pii(plain) == plain


def test_mask_pii_only_in_production(monkeypatch):
    """mask_pii returns input unchanged in non-production environments."""
    # Unset → defaults to "development"
    monkeypatch.delenv("LANGFUSE_TRACING_ENVIRONMENT", raising=False)
    text = "Email john@example.com or call 555-123-4567."
    assert mask_pii(text) == text

    # Explicit development
    monkeypatch.setenv("LANGFUSE_TRACING_ENVIRONMENT", "development")
    assert mask_pii(text) == text


# ---------------------------------------------------------------------------
# Tests: init idempotency
# ---------------------------------------------------------------------------

def test_init_observability_idempotent(monkeypatch):
    """Calling init_observability() twice does not raise."""
    # Disabled path — both calls return False, no raise
    assert init_observability() is False
    assert init_observability() is False

    # Reset and test enabled path with mocked client
    _reset_module()
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    fake = FakeLangfuseClient()
    monkeypatch.setattr(observability, "get_langfuse", lambda: fake)

    # init_observability imports pydantic_ai Agent.instrument_all; patch it
    import pydantic_ai.agent as agent_mod
    monkeypatch.setattr(agent_mod.Agent, "instrument_all", classmethod(lambda cls: None))

    assert init_observability() is True
    # Second call should be idempotent (no raise)
    assert init_observability() is True


# ---------------------------------------------------------------------------
# Tests: observe_step input/output recording
# ---------------------------------------------------------------------------

def test_observe_step_records_input_and_output(monkeypatch):
    """observe_step records input on entry and output on exit via the proxy.

    The input is passed to ``start_as_current_observation(input=...)`` on
    entry; ``proxy.set_output(...)`` calls ``span.update(output=...)``.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    fake = FakeLangfuseClient()
    monkeypatch.setattr(observability, "get_langfuse", lambda: fake)

    with observe_step("my-step", input="query text") as proxy:
        assert proxy is not None
        # On entry, input is passed to start_as_current_observation
        assert fake.observation_kwargs.get("input") == "query text"
        assert fake.observation_kwargs.get("name") == "my-step"

        proxy.set_output("result text")

    # After exit, span.update should have been called with output=
    output_updates = [u for u in fake.span.updates if "output" in u]
    assert len(output_updates) >= 1
    assert output_updates[0]["output"] == "result text"


def test_observe_step_records_error(monkeypatch):
    """An exception inside observe_step records metadata['error'] and re-raises."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")

    fake = FakeLangfuseClient()
    monkeypatch.setattr(observability, "get_langfuse", lambda: fake)

    with pytest.raises(ValueError, match="boom"):
        with observe_step("failing-step", input="x") as proxy:
            assert proxy is not None
            raise ValueError("boom")

    # The error should have been recorded in span metadata
    meta = _last_metadata(fake.span)
    assert "error" in meta
    assert "ValueError" in meta["error"]
    assert "boom" in meta["error"]


# ---------------------------------------------------------------------------
# Tests: _SpanProxy.record_usage
# ---------------------------------------------------------------------------

def test_record_usage_extracts_tokens_and_cost():
    """record_usage extracts tokens and cost from a usage object."""
    span = FakeSpan()
    proxy = _SpanProxy(span)

    usage = FakeUsage(input_tokens=100, output_tokens=50, cost=Decimal("0.003"))
    proxy.record_usage(usage, time_ms=1234.0)

    meta = _last_metadata(span)
    assert meta["tokens_in"] == 100
    assert meta["tokens_out"] == 50
    assert meta["total_tokens"] == 150
    assert meta["cost_usd"] == pytest.approx(0.003)
    assert meta["time_ms"] == 1234.0


def test_record_usage_handles_none_cost():
    """When usage.cost is None, cost_usd in metadata is None (not 0.0)."""
    span = FakeSpan()
    proxy = _SpanProxy(span)

    usage = FakeUsage(input_tokens=10, output_tokens=5, cost=None)
    proxy.record_usage(usage, time_ms=200.0)

    meta = _last_metadata(span)
    assert meta["cost_usd"] is None
    assert meta["tokens_in"] == 10
    assert meta["tokens_out"] == 5


def test_record_usage_handles_none_usage():
    """record_usage(None, ...) records zero tokens and cost_usd=None."""
    span = FakeSpan()
    proxy = _SpanProxy(span)

    proxy.record_usage(None, time_ms=500.0)

    meta = _last_metadata(span)
    assert meta["tokens_in"] == 0
    assert meta["tokens_out"] == 0
    assert meta["cost_usd"] is None
    assert meta["time_ms"] == 500.0


# ---------------------------------------------------------------------------
# Tests: _SpanProxy.record_totals
# ---------------------------------------------------------------------------

def test_record_totals():
    """record_totals attaches per-query aggregate totals to the span."""
    span = FakeSpan()
    proxy = _SpanProxy(span)

    proxy.record_totals(
        tokens_in=200,
        tokens_out=100,
        cost_usd=0.005,
        time_ms=3000.0,
    )

    meta = _last_metadata(span)
    assert meta["total_tokens_in"] == 200
    assert meta["total_tokens_out"] == 100
    assert meta["total_tokens"] == 300
    assert meta["total_cost_usd"] == pytest.approx(0.005)
    assert meta["total_time_ms"] == 3000.0
