"""Langfuse observability bootstrap and trace helpers.

Centralizes Langfuse SDK initialization and exposes small wrappers so the
LLM services and chat orchestrator can emit traces without each touching
environment variables or SDK internals directly.

Public surface:
    - ``configure_langfuse()``      — called once at app startup (fail-fast)
    - ``require_langfuse_active()`` — raise unless tracing is active; called
                                      before any grader LLM call
    - ``shutdown_langfuse()``       — called once at shutdown (flushes buffers)
    - ``langfuse_enabled()``        — True only when both keys are set
    - ``set_trace_attributes(...)`` — attach session / user / tags / metadata
                                      to the root trace of the current span
    - ``update_generation(...)``    — typed wrapper around
                                      ``Langfuse.update_current_generation``
    - ``@traced_llm_call`` /
      ``@traced_llm_stream``        — decorators for LLM service methods;
                                      wrap ``@observe`` plus request-scoped
                                      trace attribute and input propagation
                                      so new providers only need one line

Langfuse is MANDATORY for the grader: every LLM call must be traced, so
``configure_langfuse()`` aborts startup if credentials are missing or don't
authenticate, and ``require_langfuse_active()`` refuses to let a grade/rubric
call proceed untraced.  The individual helpers below still guard on
``langfuse_enabled()`` and no-op when disabled so they're safe to call from any
context (tests, scripts) without a configured client.
"""

import functools
import json
from collections.abc import Callable, Iterable
from functools import cache
from typing import Any

import structlog
from langfuse import observe
from opentelemetry import trace as otel_trace
from pydantic import BaseModel

from app.core.config import settings
from app.schemas.llm_schema import LLMRequest, LLMResponse, LLMUsage

log = structlog.stdlib.get_logger()

# Langfuse v4 reads these OpenTelemetry span attributes off the root span
# of a trace to populate the Sessions / Users / Tags views in the UI.
# Attribute names mirror the internal constants in
# langfuse/_client/attributes.py and are stable across the 4.x line.
_TRACE_SESSION_ID_ATTR = "session.id"
_TRACE_USER_ID_ATTR = "user.id"
_TRACE_TAGS_ATTR = "langfuse.trace.tags"
_TRACE_METADATA_ATTR = "langfuse.trace.metadata"


@cache
def langfuse_enabled() -> bool:
    """True when both Langfuse credentials are configured in settings."""
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


def configure_langfuse() -> None:
    """Initialize the Langfuse SDK; verify credentials best-effort.

    Instantiates the Langfuse singleton with credentials pulled directly from
    ``settings`` (the pydantic ``Settings`` class is the single source of truth —
    no environment variable propagation), then runs a best-effort ``auth_check()``.

    Langfuse is MANDATORY, enforced in layers:

    * Missing keys are a hard startup failure — the required settings in
      ``app.core.config`` won't even construct without them.
    * This function **raises** (aborting startup) if the SDK can't initialize at
      all — tracing plumbing that can't be set up is a genuine misconfiguration.
    * A failed / errored ``auth_check()`` is logged as a **warning** but does NOT
      abort startup: a Langfuse outage (or transient blip) must not block the
      grader from booting. Traces buffer and flush when Langfuse recovers.

    Runtime coverage is guaranteed regardless by :func:`require_langfuse_active`,
    which refuses any LLM call when Langfuse isn't configured.
    """
    try:
        from langfuse import Langfuse

        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    except Exception as exc:
        raise RuntimeError(
            f"Langfuse failed to initialize ({exc}). Langfuse is required — the "
            "grader refuses to start without LLM tracing. Check LANGFUSE_* config."
        ) from exc

    # Best-effort credential check: a failed / errored auth_check is a WARNING,
    # not a boot abort — Langfuse being unreachable (or a transient blip) must not
    # block the grader from starting. Traces buffer and flush when it recovers;
    # require_langfuse_active() still guarantees no LLM call runs without Langfuse
    # configured. Guarded getattr so an SDK without auth_check still boots.
    auth_check = getattr(client, "auth_check", None)
    if callable(auth_check):
        try:
            authed = auth_check()
        except Exception as exc:
            log.warning("langfuse_auth_check_errored", host=settings.langfuse_host, error=str(exc))
        else:
            if not authed:
                log.warning("langfuse_auth_check_failed", host=settings.langfuse_host)
    log.info("langfuse_configured", host=settings.langfuse_host)


def require_langfuse_active() -> None:
    """Raise unless Langfuse tracing is active — the per-request tracing gate.

    Called at the entry to every grader code path that makes an LLM call (grade
    a submission, parse a rubric) so a Gemini call is never issued untraced.
    Enforces the "no Langfuse, no LLM call" product decision at runtime, backing
    up the fail-fast startup check in :func:`configure_langfuse`.
    """
    if not langfuse_enabled():
        raise RuntimeError(
            "Langfuse is not configured — refusing to make any LLM call. Set "
            "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY (Langfuse is mandatory)."
        )


async def shutdown_langfuse() -> None:
    """Flush any pending Langfuse events on graceful shutdown."""
    if not langfuse_enabled():
        return

    try:
        from langfuse import get_client

        get_client().flush()
        log.info("langfuse_flushed")
    except Exception as exc:
        log.warning("langfuse_flush_failed", error=str(exc))


def set_trace_attributes(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
) -> None:
    """Attach session / user / tags / metadata to the current Langfuse trace.

    Langfuse reads these OTel attributes off the root span to populate
    the Sessions / Users / Tags views.  No-op when Langfuse is disabled
    or no OTel span is active (e.g. called outside an ``@observe``
    function).
    """
    if not langfuse_enabled():
        return

    span = otel_trace.get_current_span()
    if not span.is_recording():
        return

    if session_id:
        span.set_attribute(_TRACE_SESSION_ID_ATTR, session_id)
    if user_id:
        span.set_attribute(_TRACE_USER_ID_ATTR, user_id)
    if tags:
        span.set_attribute(_TRACE_TAGS_ATTR, json.dumps(tags))
    if metadata:
        span.set_attribute(_TRACE_METADATA_ATTR, json.dumps(metadata, default=str))


def set_observation_input(input: Any) -> None:
    """Set the input on the current Langfuse observation (root span).

    Use this immediately inside an ``@observe(capture_input=False)``
    function to record a curated input payload — e.g. just the user
    message — instead of letting Langfuse auto-capture every function
    argument (which can leak ``Request`` objects, auth headers,
    quota internals, etc.).  No-op when Langfuse is disabled.
    """
    if not langfuse_enabled():
        return

    try:
        from langfuse import get_client

        # Langfuse v4 SDK: ``update_current_span`` is the correct method
        # for non-generation observations (the orchestrator's @observe
        # creates a plain span, not a generation).
        get_client().update_current_span(input=input)
    except Exception as exc:
        log.warning("set_observation_input_failed", error=str(exc))


def record_trace_output(output: Any) -> None:
    """Set the output on the current Langfuse observation (root span).

    Use after completing work inside an ``@observe``-managed function to
    record a curated result payload — e.g. a graded scorecard summary —
    instead of letting Langfuse auto-capture the return value.  No-op
    when Langfuse is disabled or no span is active.
    """
    if not langfuse_enabled():
        return

    try:
        from langfuse import get_client

        get_client().update_current_span(output=output)
    except Exception as exc:
        log.warning("record_trace_output_failed", error=str(exc))


def get_current_trace_id() -> str | None:
    """Return the active Langfuse trace ID, or ``None`` if no trace.

    Resolves through ``get_client().get_current_trace_id()`` so it works
    inside any ``@observe``-managed function (relies on OTel context
    propagation).  Returns ``None`` when Langfuse is disabled or when
    called outside an active trace — callers must handle ``None``.
    """
    if not langfuse_enabled():
        return None

    try:
        from langfuse import get_client

        return get_client().get_current_trace_id()
    except Exception as exc:
        log.warning("get_current_trace_id_failed", error=str(exc))
        return None


def record_user_feedback(
    *,
    trace_id: str,
    value: int,
    comment: str | None = None,
    name: str = "user-thumbs",
) -> bool:
    """Record a thumbs-up / thumbs-down score on an existing trace.

    ``value`` must be ``1`` (positive) or ``0`` (negative); the score is
    stored with ``data_type="BOOLEAN"`` per Langfuse conventions for
    binary feedback.  ``name`` is the score identifier — defaults to
    ``"user-thumbs"`` (skill-recommended: signal-source name, lowercase
    hyphenated).  Returns ``True`` when the score was sent, ``False``
    when Langfuse is disabled or the call failed (caller decides whether
    to surface that as a 5xx or swallow silently).
    """
    if not langfuse_enabled():
        return False
    if value not in (0, 1):
        raise ValueError(f"value must be 0 or 1, got {value!r}")

    try:
        from langfuse import get_client

        get_client().create_score(
            name=name,
            value=value,
            trace_id=trace_id,
            data_type="BOOLEAN",
            comment=comment,
        )
        return True
    except Exception as exc:
        log.warning(
            "record_user_feedback_failed",
            trace_id=trace_id,
            error=str(exc),
        )
        return False


def _usage_details(usage: LLMUsage | None) -> dict[str, int] | None:
    """Convert ``LLMUsage`` into Langfuse ``usage_details`` shape."""
    if usage is None:
        return None
    details: dict[str, int] = {}
    if usage.prompt_tokens is not None:
        details["input"] = usage.prompt_tokens
    if usage.completion_tokens is not None:
        details["output"] = usage.completion_tokens
    if usage.total_tokens is not None:
        details["total"] = usage.total_tokens
    return details or None


def update_generation(
    *,
    input: Any = None,
    output: Any = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    usage: LLMUsage | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Update the current Langfuse generation observation.

    Each argument is optional; unset arguments are omitted from the
    underlying call so existing values stay untouched.  Maps the
    provider-agnostic ``LLMUsage`` onto Langfuse's ``usage_details``
    shape.  No-op when Langfuse is disabled.
    """
    if not langfuse_enabled():
        return

    from langfuse import get_client

    kwargs: dict[str, Any] = {}
    if input is not None:
        kwargs["input"] = input
    if output is not None:
        kwargs["output"] = output
    if model is not None:
        kwargs["model"] = model

    model_parameters: dict[str, Any] = {}
    if temperature is not None:
        model_parameters["temperature"] = temperature
    if max_tokens is not None:
        model_parameters["max_tokens"] = max_tokens
    if model_parameters:
        kwargs["model_parameters"] = model_parameters

    details = _usage_details(usage)
    if details:
        kwargs["usage_details"] = details
    if metadata:
        kwargs["metadata"] = metadata

    if kwargs:
        get_client().update_current_generation(**kwargs)


# ──────────────────────────────────────────────────────────────────────
# LLM service decorators
# ──────────────────────────────────────────────────────────────────────
#
# Centralizes the per-method tracing boilerplate so a new LLM provider
# only needs to apply ``@traced_llm_call`` / ``@traced_llm_stream`` to
# its public methods.  The decorators handle:
#
#   1. ``@observe(as_type="generation")`` registration
#   2. Trace attribute propagation (session/user/tags/metadata) from
#      the ``LLMRequest`` argument
#   3. Initial ``update_generation(input=, model=)`` call
#   4. Final ``update_generation(...)`` from the return value (via
#      ``extract_output``) — non-streaming only
#
# Provider methods retain control over: temperature/max_tokens with
# resolved defaults, and any provider-specific metadata (e.g. ``tools``,
# ``response_model``, ``parse_error``) — these are added by an extra
# explicit ``update_generation(...)`` call inside the method body.


def _apply_request_trace_context(request: LLMRequest) -> None:
    """Propagate ``LLMRequest.trace`` onto the current Langfuse trace.

    No-op when ``request.trace`` is ``None`` (tracing not wired by the
    caller) — consistent with the "missing credentials = silently
    disabled" posture of the rest of this module.
    """
    trace = request.trace
    if trace is None:
        return
    set_trace_attributes(
        session_id=trace.session_id,
        user_id=trace.user_id,
        metadata=trace.metadata,
        tags=trace.tags,
    )


def _request_input(request: LLMRequest) -> Any:
    """Build a Langfuse-friendly ``input`` payload from an ``LLMRequest``.

    Includes ``system_prompt`` when set so the trace shows the full
    instruction the model received — important for prompts assembled
    from server-side templates (planner, curator) where the user prompt
    alone is insufficient to reproduce the call.
    """
    if request.messages:
        messages = [m.model_dump(exclude_none=True) for m in request.messages]
        if request.system_prompt:
            return [
                {"role": "system", "content": request.system_prompt},
                *messages,
            ]
        return messages
    if request.system_prompt:
        return {
            "system_prompt": request.system_prompt,
            "prompt": request.prompt,
        }
    return request.prompt


def _default_response_extract(result: Any) -> dict[str, Any]:
    """Default output extractor for methods returning ``LLMResponse``.

    Pulls ``content`` / ``model`` / ``usage`` off the response so the
    decorator can record them on the generation span.  Returns ``{}`` if
    the result isn't an ``LLMResponse``, so the decorator skips the
    trailing ``update_generation`` call.
    """
    if isinstance(result, LLMResponse):
        return {
            "output": result.content,
            "model": result.model,
            "usage": result.usage,
        }
    return {}


def structured_response_extract(result: Any) -> dict[str, Any]:
    """Output extractor for methods returning a parsed ``BaseModel``.

    Records the parsed model as the generation output.  ``usage`` is not
    recoverable from a bare ``BaseModel`` — provider methods should call
    ``update_generation(usage=...)`` explicitly after the LLM call.
    """
    if isinstance(result, BaseModel):
        return {"output": result.model_dump()}
    return {}


def traced_llm_call(
    *,
    name: str,
    extract_output: Callable[[Any], dict[str, Any]] = _default_response_extract,
) -> Callable:
    """Decorator for async LLM service methods (non-streaming).

    Stacks ``@observe(as_type="generation", capture_input=False,
    capture_output=False)`` and adds request-scoped trace plumbing so
    the decorated method only needs to focus on its LLM call.

    Required signature:
        ``async def method(self, request: LLMRequest, *args, **kwargs) -> Any``

    Requires ``self._model`` to hold the resolved model name.  No-op
    when Langfuse is disabled (the inner helpers all guard).
    """

    def decorator(fn: Callable) -> Callable:
        @observe(
            as_type="generation",
            name=name,
            capture_input=False,
            capture_output=False,
        )
        @functools.wraps(fn)
        async def wrapper(self: Any, request: LLMRequest, *args: Any, **kwargs: Any) -> Any:
            _apply_request_trace_context(request)
            update_generation(
                input=_request_input(request),
                model=getattr(self, "_model", None),
            )
            result = await fn(self, request, *args, **kwargs)
            extracted = extract_output(result)
            if extracted:
                update_generation(**extracted)
            return result

        return wrapper

    return decorator


def emit_tool_span(
    *,
    tool_name: str,
    round_index: int,
    latency_ms: float,
    status: str,
    is_stub: bool = False,
    error_class: str | None = None,
    error_message: str | None = None,
    input: Any = None,
    output: Any = None,
) -> None:
    """Emit a Langfuse child span for a single tool invocation.

    Nested under the currently active ``@observe``-managed generation span.
    No-op when Langfuse is disabled.  Uses ``client.span(...)`` synchronously
    (not an async context manager) to avoid OTel context-propagation edge
    cases inside async-generator frames.
    """
    if not langfuse_enabled():
        return

    metadata: dict[str, Any] = {
        "round_index": round_index,
        "latency_ms": round(latency_ms, 2),
        "status": status,
        "is_stub": is_stub,
    }
    if error_class is not None:
        metadata["error_class"] = error_class
    if error_message is not None:
        metadata["error_message"] = error_message

    try:
        from langfuse import get_client

        span_kwargs: dict[str, Any] = {
            "name": tool_name,
            "as_type": "tool",
            "metadata": metadata,
            "level": "ERROR" if status == "error" else "DEFAULT",
        }
        if input is not None:
            span_kwargs["input"] = input
        if output is not None:
            span_kwargs["output"] = output
        span = get_client().start_observation(**span_kwargs)
        span.end()
    except Exception as exc:
        log.warning("emit_tool_span_failed", tool_name=tool_name, error=str(exc))


def emit_pinecone_span(
    *,
    name: str,
    namespace: str,
    top_k: int,
    pc_filter: dict[str, Any] | None,
    caller: str,
    latency_ms: float,
    status: str,
    matches: list[dict[str, Any]] | None = None,
    query_preview: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    """Emit a Langfuse child span for a single Pinecone retrieval call.

    Records the full match payload (id, score, metadata) as the span
    output so operators can inspect exactly what came back from
    Pinecone for any trace.  Nested under the currently active
    ``@observe``-managed observation when one exists; otherwise emits
    as a top-level span.  No-op when Langfuse is disabled.

    ``status`` is ``"ok"`` or ``"error"``; error spans are flagged with
    ``level="ERROR"`` so they surface in the Langfuse UI's error views.
    """
    if not langfuse_enabled():
        return

    input_payload: dict[str, Any] = {
        "namespace": namespace,
        "top_k": top_k,
        "caller": caller,
    }
    if pc_filter is not None:
        input_payload["filter"] = pc_filter
    if query_preview is not None:
        input_payload["query"] = query_preview

    metadata: dict[str, Any] = {
        "latency_ms": round(latency_ms, 2),
        "status": status,
        "candidate_count": len(matches) if matches is not None else 0,
    }
    if error_class is not None:
        metadata["error_class"] = error_class
    if error_message is not None:
        metadata["error_message"] = error_message

    try:
        from langfuse import get_client

        span_kwargs: dict[str, Any] = {
            "name": name,
            "as_type": "retriever",
            "input": input_payload,
            "metadata": metadata,
            "level": "ERROR" if status == "error" else "DEFAULT",
        }
        if matches is not None:
            span_kwargs["output"] = {"matches": matches}
        span = get_client().start_observation(**span_kwargs)
        span.end()
    except Exception as exc:
        log.warning("emit_pinecone_span_failed", name=name, error=str(exc))


def emit_generation_span(
    *,
    name: str,
    model: str | None = None,
    usage_details: dict[str, int] | None = None,
    input: Any = None,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Emit a Langfuse generation observation recording model + token usage.

    For callers that make raw provider-SDK calls (e.g. the AP FRQ grader's
    google-genai calls running in worker threads) instead of going through the
    ``@traced_llm_call`` decorators.  Records ``model`` + ``usage_details`` so
    Langfuse attributes a cost to the call.  Nested under the currently active
    ``@observe`` span when one is in context; otherwise a top-level generation.
    No-op when Langfuse is disabled.
    """
    if not langfuse_enabled():
        return

    try:
        from langfuse import get_client

        span_kwargs: dict[str, Any] = {"name": name, "as_type": "generation"}
        if model is not None:
            span_kwargs["model"] = model
        if usage_details:
            span_kwargs["usage_details"] = usage_details
        if input is not None:
            span_kwargs["input"] = input
        if output is not None:
            span_kwargs["output"] = output
        if metadata:
            span_kwargs["metadata"] = metadata
        span = get_client().start_observation(**span_kwargs)
        span.end()
    except Exception as exc:
        log.warning("emit_generation_span_failed", name=name, error=str(exc))


def traced_llm_stream(
    *,
    name: str,
    transform_to_string: Callable[[Iterable[Any]], Any] | None = None,
) -> Callable:
    """Decorator for async-generator LLM service methods (streaming).

    Stacks ``@observe(as_type="generation", capture_input=False,
    transform_to_string=...)``.  Output capture across yields is
    delegated to Langfuse via ``transform_to_string`` because the
    generator only finishes when the consumer stops iterating —
    ``update_generation(output=...)`` at function return wouldn't
    capture the streamed chunks.

    Required signature:
        ``async def method(self, request: LLMRequest, *args, **kwargs)
            -> AsyncIterator[...]``
    """

    def decorator(fn: Callable) -> Callable:
        observe_kwargs: dict[str, Any] = {
            "as_type": "generation",
            "name": name,
            "capture_input": False,
        }
        if transform_to_string is not None:
            observe_kwargs["transform_to_string"] = transform_to_string

        @observe(**observe_kwargs)
        @functools.wraps(fn)
        async def wrapper(self: Any, request: LLMRequest, *args: Any, **kwargs: Any) -> Any:
            _apply_request_trace_context(request)
            update_generation(
                input=_request_input(request),
                model=getattr(self, "_model", None),
            )
            async for event in fn(self, request, *args, **kwargs):
                yield event

        return wrapper

    return decorator
