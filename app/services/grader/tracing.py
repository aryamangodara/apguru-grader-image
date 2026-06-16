"""Bridge the grader's ``on_response`` hook to Langfuse cost tracing.

The vendored grader calls google-genai directly and exposes an optional
``on_response`` hook (invoked with the raw response after each billed call). This
turns that hook into a Langfuse generation recording the model and token usage,
so the grader's OCR / rubric-parse / typed-label / grading calls show up with
cost — reusing ``app/core/observability.py``.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

import structlog

from app.core.observability import emit_generation_span

log = structlog.get_logger(__name__)


def _usage_details(response: Any) -> dict[str, int] | None:
    """Map google-genai ``usage_metadata`` -> Langfuse ``usage_details`` (tokens)."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return None
    prompt = int(getattr(usage, "prompt_token_count", None) or 0)
    candidates = int(getattr(usage, "candidates_token_count", None) or 0)
    thoughts = int(getattr(usage, "thoughts_token_count", None) or 0)
    total = getattr(usage, "total_token_count", None)

    details: dict[str, int] = {}
    if prompt:
        details["input"] = prompt
    output = candidates + thoughts
    if output:
        details["output"] = output
    if total is not None:
        details["total"] = int(total)
    return details or None


def gemini_generation_reporter(name: str, model: str) -> Callable[[Any], None]:
    """Build an ``on_response`` hook that records one Langfuse generation.

    ``name`` tags the phase (e.g. ``grader.ocr``, ``grader.grade``); ``model`` is
    the Gemini model so Langfuse can price it. The hook is a no-op when Langfuse
    is disabled and never raises (the grader swallows hook errors, but we stay
    defensive anyway).
    """

    def _report(response: Any) -> None:
        try:
            emit_generation_span(
                name=name,
                model=model,
                usage_details=_usage_details(response),
            )
        except Exception as exc:  # pragma: no cover - tracing must never break grading
            log.warning("gemini_generation_reporter_failed", name=name, error=str(exc))

    return _report
