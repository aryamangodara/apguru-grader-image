"""Bridge the grader's ``on_response`` hook to Langfuse cost and prompt tracing.

The vendored grader calls google-genai directly and exposes an optional
``on_response`` hook invoked after each billed response. This records one
Langfuse generation per call including:

- model + token usage (for cost attribution)
- prompt text (text-only parts; image bytes are stripped so PDF page renders
  don't bloat traces)
- model's raw text output (the JSON the model returned, for prompt analysis)
- finish reason and thinking-token count (for reasoning-model debugging)
- per-call label from ``generate_with_retry`` (e.g. ``"grade 1a"``) as
  metadata so individual question calls are distinguishable

The hook is a no-op when Langfuse is disabled and never raises — tracing
must never break grading.
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


def _extract_text_contents(contents: Any) -> list[str] | None:
    """Extract only the string parts from Gemini request contents.

    Rubric-parse and OCR calls interleave text strings (the system prompt,
    page-label strings like ``"[Answer page 2/4]"``) with rendered PDF pages
    as ``types.Part`` image blobs. Only the strings are useful in Langfuse —
    the image blobs are silently dropped so traces stay compact.
    """
    if not contents:
        return None
    parts = [item for item in contents if isinstance(item, str)]
    return parts if parts else None


def _extract_response_text(response: Any) -> str | None:
    """Return the model's text output (raw JSON for structured-output calls)."""
    try:
        text = getattr(response, "text", None)
        return text if text else None
    except Exception:
        return None


def _extract_finish_reason(response: Any) -> str | None:
    try:
        candidates = getattr(response, "candidates", None)
        if candidates:
            return str(getattr(candidates[0], "finish_reason", None))
    except Exception:
        pass
    return None


def gemini_generation_reporter(name: str, model: str) -> Callable[..., None]:
    """Build an ``on_response`` hook that records one Langfuse generation.

    ``name`` tags the pipeline phase (e.g. ``grader.ocr``, ``grader.grade``);
    ``model`` is the Gemini model so Langfuse can price the call.

    The hook signature (as called by ``generate_with_retry``) is::

        hook(response, contents=None, label="")

    - ``contents``  — the request contents; text parts are captured as ``input``
                      and image Parts are stripped
    - ``label``     — the per-call label from ``generate_with_retry``
                      (e.g. ``"grade 1a"``), stored in metadata so individual
                      question grading calls are distinguishable in Langfuse
    """

    def _report(response: Any, contents: Any = None, label: str = "") -> None:
        try:
            usage = _usage_details(response)
            input_parts = _extract_text_contents(contents)
            output_text = _extract_response_text(response)
            finish_reason = _extract_finish_reason(response)

            metadata: dict[str, Any] = {}
            if finish_reason:
                metadata["finish_reason"] = finish_reason
            if label:
                metadata["label"] = label
            thinking = int(
                getattr(
                    getattr(response, "usage_metadata", None) or object(),
                    "thoughts_token_count",
                    0,
                )
                or 0
            )
            if thinking:
                metadata["thinking_tokens"] = thinking

            emit_generation_span(
                name=name,
                model=model,
                usage_details=usage,
                input=input_parts,
                output=output_text,
                metadata=metadata or None,
            )
        except Exception as exc:  # pragma: no cover - tracing must never break grading
            log.warning("gemini_generation_reporter_failed", name=name, error=str(exc))

    return _report
