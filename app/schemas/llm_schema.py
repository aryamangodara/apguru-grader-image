from typing import Any

from pydantic import BaseModel


class LLMUsage(BaseModel):
    """Token usage stats (provider-agnostic)."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ChatMessage(BaseModel):
    """A single message in a multi-turn conversation."""

    role: str  # "user" | "assistant" | "system"
    content: str | None = None


class TraceContext(BaseModel):
    """Observability context attached to an ``LLMRequest`` by composition.

    When set on an ``LLMRequest``, the LLM service propagates these
    fields onto the current Langfuse trace (session / user / tags /
    metadata) so the trace groups correctly in the Langfuse UI.  Kept
    in a dedicated class — rather than flattened into ``LLMRequest`` —
    so the request schema stays focused on LLM concerns and new trace
    fields can be added without churning every ``LLMRequest`` caller.

    Tracing silently no-ops when ``LLMRequest.trace`` is ``None`` or
    when Langfuse is disabled.
    """

    session_id: str | None = None
    user_id: str | None = None
    metadata: dict[str, Any] | None = None
    tags: list[str] | None = None


class LLMRequest(BaseModel):
    """Input to an LLM call.

    For single-turn calls, use ``prompt`` (and optionally ``system_prompt``).
    For multi-turn conversations, use ``messages`` instead — when set,
    ``prompt`` is ignored and the conversation is sent as-is.

    Attach ``trace`` to propagate observability context (session, user,
    tags, metadata) onto the Langfuse trace.  Leave unset to skip
    tracing metadata entirely.
    """

    prompt: str = ""
    system_prompt: str | None = None
    messages: list[ChatMessage] | None = None
    temperature: float | None = None
    max_tokens: int | None = None
    thinking_budget: int | None = None
    grounding: bool = False
    tools: list[str] | None = None
    max_tool_rounds: int = 10

    trace: TraceContext | None = None


class LLMResponse(BaseModel):
    """Raw text response from an LLM."""

    content: str
    model: str
    provider: str
    usage: LLMUsage | None = None
    tools_called: list[str] = []


class SuggestionsOutput(BaseModel):
    """Structured output for the post-stream chip-suggestion call.

    The orchestrator runs a small ``generate_structured`` call after the
    main reply has finished streaming and emits the resulting ``chips``
    over SSE so the chat UI can render tappable follow-up prompts above
    the input.
    """

    chips: list[str]


class AutoTagOutput(BaseModel):
    """Structured output for the post-stream session auto-tag call.

    The classifier returns a JSON array of 0-2 preset tag slugs from the
    fixed list in app/prompts/auto_tag_prompt.PRESET_TAGS.
    """

    tags: list[str]
