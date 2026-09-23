"""Shared pytest fixtures."""

import pytest

from lab_orchestrator.core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings()` is `lru_cache`d for normal request-time reuse;
    without this, one test's monkeypatched env vars would leak into
    whichever test runs next and also calls it.
    """
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
