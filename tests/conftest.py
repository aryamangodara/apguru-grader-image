"""Shared fixtures for the grader-only test suite."""

from __future__ import annotations

import os

# The Settings singleton fires on first import of any app module — provide dummy
# credentials so the import doesn't fail when no .env is present. Langfuse keys
# are required now (Langfuse is mandatory); the lifespan never runs under
# ASGITransport so no real Langfuse client is initialized from these dummies.
os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_USER", "test")
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("DB_NAME", "test_db")
os.environ.setdefault("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
os.environ.setdefault("LANGFUSE_SECRET_KEY", "sk-lf-test")
# Keep the SDK from shipping spans to Langfuse Cloud with the dummy keys above
# (would 401 in a background flush). Our app's langfuse_enabled() still reads the
# keys as present, so the mandatory-Langfuse enforcement is exercised unchanged.
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client():
    """Async HTTP client wired to the FastAPI app with the DB fully mocked.

    The grader and health endpoints are public (no auth), so no token is
    attached. ``ASGITransport`` does not run the app lifespan, so the startup DB
    connect / grader-job reaper never fire — only the patched singleton is used.
    """
    mock_db = AsyncMock()
    mock_db.connect = AsyncMock(return_value=True)
    mock_db.dispose = AsyncMock()

    with (
        patch("app.core.database.Database.get_instance", return_value=mock_db),
        patch("app.core.database.Database.dispose_all", new_callable=AsyncMock),
    ):
        from app.main import create_app

        app = create_app()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac
