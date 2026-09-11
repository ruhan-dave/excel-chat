"""Pytest configuration — disable Langfuse in unit tests by default.

Set LANGFUSE_ENABLED_IN_TESTS=1 to enable Langfuse for integration tests.
"""
import os
import pytest


_LANGFUSE_VARS = [
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_BASE_URL",
    "LANGFUSE_HOST",
]


def _strip_langfuse_env() -> dict[str, str]:
    """Remove LANGFUSE_* env vars and return saved values for restoration."""
    saved = {}
    for var in _LANGFUSE_VARS:
        if var in os.environ:
            saved[var] = os.environ[var]
            del os.environ[var]
    return saved


def _restore_langfuse_env(saved: dict[str, str]) -> None:
    for var, val in saved.items():
        os.environ[var] = val


def _reset_observability_module():
    """Reset the observability module's cached state."""
    try:
        import observability
        observability._initialized = False
        observability._client = None
        observability._disabled_reason = None
    except ImportError:
        pass


@pytest.fixture(autouse=True, scope="session")
def disable_langfuse_session():
    """Strip LANGFUSE_* env vars before test collection unless explicitly enabled."""
    if os.environ.get("LANGFUSE_ENABLED_IN_TESTS") == "1":
        yield
        return
    saved = _strip_langfuse_env()
    yield
    _restore_langfuse_env(saved)


@pytest.fixture(autouse=True, scope="function")
def _isolate_observability_per_test():
    """Per-test isolation: strip keys + reset module state.

    This catches cases where load_dotenv() in another test module re-loads
    keys from .env after the session fixture stripped them, or where
    pipeline.py's module-level init_observability() cached a client.
    """
    if os.environ.get("LANGFUSE_ENABLED_IN_TESTS") == "1":
        yield
        _reset_observability_module()
        return
    saved = _strip_langfuse_env()
    _reset_observability_module()
    yield
    _reset_observability_module()
    _restore_langfuse_env(saved)
