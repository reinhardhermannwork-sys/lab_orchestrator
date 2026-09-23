"""Shared pytest fixtures."""

import pytest

from lab_orchestrator.core.config import get_settings
from lab_orchestrator.db.database import get_engine


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """`get_settings()` and `get_engine()` are `lru_cache`d for normal
    request-time reuse; without this, one test's monkeypatched env vars
    would leak into whichever test runs next and also calls them.
    """
    get_settings.cache_clear()
    get_engine.cache_clear()
    yield
    get_settings.cache_clear()
    get_engine.cache_clear()
