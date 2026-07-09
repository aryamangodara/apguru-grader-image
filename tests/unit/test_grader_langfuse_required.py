"""Langfuse is mandatory — no Langfuse, no LLM call.

Covers the two enforcement points:
  * ``require_langfuse_active()`` — the per-request gate that refuses an LLM
    call when tracing is disabled, and that ``_do_grade`` calls it first.
  * ``configure_langfuse()`` — the startup fail-fast that aborts boot when the
    SDK can't initialize or the credentials don't authenticate.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.core import observability
from app.services import grader_job_service

# --- the per-request gate ----------------------------------------------------

def test_require_langfuse_active_raises_when_disabled(monkeypatch):
    monkeypatch.setattr(observability, "langfuse_enabled", lambda: False)
    with pytest.raises(RuntimeError, match="refusing to make any LLM call"):
        observability.require_langfuse_active()


def test_require_langfuse_active_passes_when_enabled(monkeypatch):
    monkeypatch.setattr(observability, "langfuse_enabled", lambda: True)
    observability.require_langfuse_active()  # must not raise


async def test_do_grade_refuses_before_any_work_when_disabled(monkeypatch):
    """The gate is the first thing _do_grade does — it raises before touching the DB."""
    monkeypatch.setattr(
        grader_job_service, "require_langfuse_active",
        MagicMock(side_effect=RuntimeError("Langfuse is not configured")),
    )
    # No DB mock needed: the guard fires before Database.get_instance().
    with pytest.raises(RuntimeError, match="Langfuse is not configured"):
        await grader_job_service._do_grade("job-key")


# --- the startup fail-fast ----------------------------------------------------

def test_configure_langfuse_raises_on_init_failure(monkeypatch):
    monkeypatch.setattr("langfuse.Langfuse", MagicMock(side_effect=Exception("bad host")))
    with pytest.raises(RuntimeError, match="failed to initialize"):
        observability.configure_langfuse()


def test_configure_langfuse_raises_on_auth_failure(monkeypatch):
    client = MagicMock()
    client.auth_check.return_value = False
    monkeypatch.setattr("langfuse.Langfuse", MagicMock(return_value=client))
    with pytest.raises(RuntimeError, match="auth check failed"):
        observability.configure_langfuse()


def test_configure_langfuse_ok_when_auth_passes(monkeypatch):
    client = MagicMock()
    client.auth_check.return_value = True
    monkeypatch.setattr("langfuse.Langfuse", MagicMock(return_value=client))
    observability.configure_langfuse()  # must not raise
