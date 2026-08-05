"""Content-map typed answers onto rubric questions — APGuru extension.

Typed submissions arrive as ``{key: answer_text}``. Normally the key IS the rubric
question id and the grader matches by an exact dict lookup. When a client keys the
answers by its own local numbering instead (e.g. ``"21".."24"`` for a rubric whose
ids are ``"216a".."220"``), that lookup finds nothing and every question scores 0.

This module makes ONE Gemini call that matches each submitted answer to the rubric
question(s) it actually answers — by content, using each question's stem
(``prompt_summary``) and criteria — so the grade lands on the right questions. The
caller (``grader_answer_map_service``) validates the result and fails the job loudly
if a match is low-confidence, collides, or matches nothing; it never guesses.

Like ``knowledge.py``, this is specific to the dashboard and is NOT synced from the
source notebook — keep it here.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from google import genai
from google.genai import types

from .core import _diagnose_empty, _normalize_qid, generate_with_retry
from .schemas import AnswerQuestionMap, ParsedRubric

_ANSWER_MAP_PROMPT = Path(__file__).resolve().parent / "prompts" / "answer_map.txt"

# Answer text is included verbatim so the model can judge content, but a runaway blob
# would bloat the prompt for no gain — the opening is more than enough to identify it.
_MAX_ANSWER_CHARS = 4000


def map_answers_to_questions(
    client: genai.Client,
    *,
    answers: dict[str, str],
    rubric: ParsedRubric,
    model: str = "gemini-3.5-flash",
    prompt_path: Path = _ANSWER_MAP_PROMPT,
    on_response: Callable[..., None] | None = None,
) -> AnswerQuestionMap:
    """Match each submitted answer to the rubric question(s) it addresses, by content.

    ``answers`` maps the submitted key to the student's answer text. ``rubric`` is the
    cached marking scheme. Returns an :class:`AnswerQuestionMap` with one entry per
    submitted answer (question ids drawn only from the rubric, plus a confidence). The
    call is deterministic (temperature 0); ``on_response`` receives the raw response for
    Langfuse tracing. Validation / fail-loud lives in the calling service, not here.
    """
    prompt = Path(prompt_path).read_text(encoding="utf-8")

    contents: list = [prompt]
    contents.append("\n=== RUBRIC QUESTIONS — match answers to these ids only ===\n")
    for q in rubric.questions:
        qid = _normalize_qid(q.question_id)
        criteria = " | ".join(p.criterion for p in q.rubric_points if p.criterion)
        block = f"[{qid}] ({q.max_points}pt) {q.prompt_summary}".rstrip()
        if criteria:
            block += f"\n    criteria: {criteria}"
        contents.append(block)

    contents.append("\n=== STUDENT ANSWERS — one mapping entry per answer, in this order ===\n")
    for key, text in answers.items():
        snippet = (text or "").strip()
        if len(snippet) > _MAX_ANSWER_CHARS:
            snippet = snippet[:_MAX_ANSWER_CHARS] + " […truncated]"
        contents.append(f"[answer key: {key}]\n{snippet}\n")

    response = generate_with_retry(
        client,
        label="answer-map",
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AnswerQuestionMap,
            temperature=0,
        ),
        on_response=on_response,
    )

    parsed = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini returned no parsed AnswerQuestionMap ({_diagnose_empty(response)}). "
            "Raw text:\n" + (response.text or "<empty>")
        )
    return parsed  # type: ignore[return-value]
