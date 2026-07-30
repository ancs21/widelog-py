from __future__ import annotations

from typing import Any

import pytest

import widelog
from widelog import init


@pytest.fixture
def seen() -> Any:
    """Capture emitted events instead of writing NDJSON to stdout."""
    saved = dict(widelog._config)
    captured: list[dict[str, Any]] = []
    init(service="checkout", environment="test", sink=captured.append)
    yield captured
    widelog._config.clear()
    widelog._config.update(saved)


@pytest.fixture
def cold() -> Any:
    """Each test gets a fresh container, as far as cold-start detection knows."""
    widelog._cold_start = True
    yield
    widelog._cold_start = True
