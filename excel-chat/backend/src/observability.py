"""Langfuse observability layer for RagSheets.

Provides graceful-degradation wrappers around Langfuse v4 SDK + Pydantic AI
OpenTelemetry instrumentation.  When ``LANGFUSE_PUBLIC_KEY`` /
``LANGFUSE_SECRET_KEY`` are absent every helper becomes a no-op so the
application runs exactly as it would without observability.

Public API
----------
- ``is_observability_enabled()`` — quick env check
- ``init_observability()`` — idempotent; calls ``Agent.instrument_all()``
- ``observe_agent_run(...)`` — context manager: trace-level span + attributes
- ``observe_step(...)`` — context manager: step/tool/decision span with
  input-on-entry / output-on-exit / error-on-exception recording
- ``mask_pii(text)`` — email/phone redaction (production only)
- ``flush_observability()`` — export in-flight traces (FastAPI shutdown)
"""

from __future__ import annotations

import os
import re
import json
from contextlib import contextmanager
from typing import Any, Generator, Optional

# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

_client: Any = None          # cached langfuse client (or None when disabled)
_initialized: bool = False   # idempotency guard for init_observability
_disabled_reason: str | None = None

LANGFUSE_ENV_VARS = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"]

# PII regexes (compiled once)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")

_MAX_IO_LEN = 500  # truncate span input/output to avoid token bloat


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _truncate(value: Any, max_len: int = _MAX_IO_LEN) -> Any:
    """Truncate strings / JSON-serialised values to *max_len* chars."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:max_len] + f"\n... (truncated, {len(value)} total chars)" if len(value) > max_len else value
    try:
        s = json.dumps(value, default=str)
        if len(s) > max_len:
            return s[:max_len] + f"\n... (truncated, {len(s)} total chars)"
        return value
    except (TypeError, ValueError):
        s = str(value)
        return s[:max_len] if len(s) > max_len else value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_observability_enabled() -> bool:
    """True iff the minimum Langfuse env vars are present."""
    return bool(
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def get_langfuse() -> Any:
    """Return the cached Langfuse client, or ``None`` when disabled.

    Initialisation failures are swallowed — observability must never crash
    the application.  On failure the reason is stored in ``_disabled_reason``
    so callers can inspect it.
    """
    global _client, _disabled_reason
    if _client is not None:
        return _client
    if not is_observability_enabled():
        _disabled_reason = "LANGFUSE_* keys missing"
        return None
    try:
        from langfuse import get_client as _lf_get_client
        _client = _lf_get_client()
        # Verify auth
        if hasattr(_client, "auth_check"):
            if not _client.auth_check():
                _disabled_reason = "Langfuse auth_check failed"
                _client = None
                return None
    except Exception as exc:
        _disabled_reason = f"Langfuse init error: {type(exc).__name__}: {exc}"
        _client = None
        return None
    return _client


def init_observability() -> bool:
    """Initialise Pydantic AI OTel instrumentation (idempotent).

    Must be called **before** any ``Agent(...)`` is constructed.
    Returns ``True`` when instrumentation was enabled, ``False`` when
    disabled (keys missing or init failure).
    """
    global _initialized
    if _initialized:
        return is_observability_enabled()
    _initialized = True

    if not is_observability_enabled():
        print("⚠️ Langfuse observability disabled (LANGFUSE_* keys missing)")
        return False

    client = get_langfuse()
    if client is None:
        print(f"⚠️ Langfuse observability disabled ({_disabled_reason})")
        return False

    try:
        from pydantic_ai.agent import Agent
        Agent.instrument_all()
        print("✅ Langfuse observability enabled — Pydantic AI instrumentation active")
        return True
    except Exception as exc:
        print(f"⚠️ Langfuse instrumentation failed: {type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------------------
# Span proxy (returned by observe_step)
# ---------------------------------------------------------------------------

class _SpanProxy:
    """Lightweight proxy wrapping a Langfuse span for incremental updates.

    When observability is disabled the proxy is still yielded (as ``None``)
    so call sites can ``if proxy: proxy.record(...)``.
    """

    __slots__ = ("_span", "_metadata")

    def __init__(self, span: Any) -> None:
        self._span = span
        self._metadata: dict[str, Any] = {}

    def _safe_update(self, **kwargs: Any) -> None:
        try:
            self._span.update(**kwargs)
        except Exception as exc:
            print(f"⚠️ Langfuse span update failed: {exc}")

    def set_output(self, output: Any) -> None:
        """Record the step's output value (truncated + PII-masked)."""
        self._safe_update(output=_truncate(mask_pii(output)))

    def record(self, decision: str | None = None, output: Any = None, **extra: Any) -> None:
        """Attach decision metadata + optional output to the span."""
        if decision is not None:
            self._metadata["decision"] = decision
        self._metadata.update(extra)
        if output is not None:
            self.set_output(output)
        self._safe_update(metadata=dict(self._metadata))

    def record_usage(self, usage: Any, time_ms: float) -> None:
        """Extract token/cost data from a pydantic-ai ``RunUsage`` object.

        Handles ``usage=None`` (pure-Python stages with no LLM) by recording
        zero tokens and ``cost_usd: null``.

        Records both in ``metadata`` (for analytics/querying) AND in the
        native Langfuse ``usage_details``/``cost_details`` fields (for the
        UI cost column and cost dashboards).
        """
        tokens_in = 0
        tokens_out = 0
        cost_usd: float | None = None
        input_cost: float | None = None
        output_cost: float | None = None

        if usage is not None:
            try:
                tokens_in = getattr(usage, "input_tokens", 0) or 0
                tokens_out = getattr(usage, "output_tokens", 0) or 0
                cost = getattr(usage, "cost", None)
                if cost is not None:
                    cost_usd = float(cost)
            except Exception:
                pass

        self._metadata["tokens_in"] = tokens_in
        self._metadata["tokens_out"] = tokens_out
        self._metadata["total_tokens"] = tokens_in + tokens_out
        self._metadata["cost_usd"] = cost_usd
        self._metadata["time_ms"] = round(time_ms, 2)

        # Native Langfuse fields (visible in UI cost column + dashboards)
        usage_details = {"input": tokens_in, "output": tokens_out}
        cost_details: dict[str, float] = {}
        if cost_usd is not None:
            cost_details = {"total": cost_usd}

        try:
            self._span.update(
                metadata=dict(self._metadata),
                usage_details=usage_details,
                cost_details=cost_details if cost_details else None,
            )
        except Exception as exc:
            print(f"⚠️ Langfuse span update failed: {exc}")

    def record_totals(
        self,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float | None,
        time_ms: float,
    ) -> None:
        """Attach per-query aggregate totals to the trace-level span.

        Records both in ``metadata`` (for analytics/querying) AND in the
        native Langfuse ``usage_details``/``cost_details`` fields (for the
        UI cost column and cost dashboards).
        """
        self._metadata["total_tokens_in"] = tokens_in
        self._metadata["total_tokens_out"] = tokens_out
        self._metadata["total_tokens"] = tokens_in + tokens_out
        self._metadata["total_cost_usd"] = cost_usd
        self._metadata["total_time_ms"] = round(time_ms, 2)

        # Native Langfuse fields (visible in UI cost column + dashboards)
        usage_details = {"input": tokens_in, "output": tokens_out}
        cost_details: dict[str, float] = {}
        if cost_usd is not None:
            cost_details = {"total": cost_usd}

        try:
            self._span.update(
                metadata=dict(self._metadata),
                usage_details=usage_details,
                cost_details=cost_details if cost_details else None,
            )
        except Exception as exc:
            print(f"⚠️ Langfuse span update failed: {exc}")


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------

@contextmanager
def observe_agent_run(
    name: str,
    user_id: str = "anonymous",
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Generator[Optional[_SpanProxy], None, None]:
    """Trace-level span + propagated attributes (user/session/tags/metadata).

    No-op when observability is disabled (yields ``None``).
    """
    client = get_langfuse()
    if client is None:
        yield None
        return

    try:
        from langfuse import propagate_attributes
    except Exception:
        propagate_attributes = None  # type: ignore[assignment]

    try:
        with client.start_as_current_observation(
            as_type="span",
            name=name,
            metadata=metadata,
        ) as span:
            if propagate_attributes is not None:
                try:
                    with propagate_attributes(
                        user_id=user_id,
                        session_id=session_id,
                        tags=tags or [],
                        metadata=metadata or {},
                    ):
                        yield _SpanProxy(span)
                except Exception:
                    yield _SpanProxy(span)
            else:
                yield _SpanProxy(span)
    except Exception as exc:
        print(f"⚠️ Langfuse observe_agent_run failed: {exc}")
        yield None


@contextmanager
def observe_step(
    name: str,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Generator[Optional[_SpanProxy], None, None]:
    """Step / tool / decision span.

    Records ``input`` on entry (truncated + PII-masked) and allows the caller
    to record ``output`` / ``decision`` / ``usage`` via the yielded proxy.

    On exception the error is attached to span metadata before re-raising.

    No-op when observability is disabled (yields ``None``).
    """
    client = get_langfuse()
    if client is None:
        yield None
        return

    proxy: _SpanProxy | None = None
    try:
        with client.start_as_current_observation(
            as_type="span",
            name=name,
            input=_truncate(mask_pii(input)),
            metadata=metadata,
        ) as span:
            proxy = _SpanProxy(span)
            if metadata:
                proxy._metadata.update(metadata)
            yield proxy
    except Exception as exc:
        # Record error on the span if we have one, then re-raise
        if proxy is not None:
            try:
                proxy._metadata["error"] = f"{type(exc).__name__}: {exc}"
                proxy._safe_update(metadata=dict(proxy._metadata))
            except Exception:
                pass
        raise


# ---------------------------------------------------------------------------
# PII masking
# ---------------------------------------------------------------------------

def mask_pii(text: Any) -> Any:
    """Redact emails and phone numbers (production only).

    In non-production environments (``LANGFUSE_TRACING_ENVIRONMENT != 'production'``)
    the text is returned unchanged so developers see raw data for debugging.
    """
    if not isinstance(text, str):
        return text
    env = os.environ.get("LANGFUSE_TRACING_ENVIRONMENT", "development")
    if env != "production":
        return text
    text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _PHONE_RE.sub("[PHONE]", text)
    return text


# ---------------------------------------------------------------------------
# Flush
# ---------------------------------------------------------------------------

def flush_observability() -> None:
    """Flush in-flight traces to Langfuse (call on FastAPI shutdown)."""
    client = get_langfuse()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:
        print(f"⚠️ Langfuse flush failed: {exc}")
