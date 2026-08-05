"""Rubric-free "knowledge" grading — APGuru extension (no upstream in notebooks/Grader).

Homework can be registered WITHOUT a marking scheme. There is then no rubric to grade
against, so this module asks Gemini to judge each answer right/wrong from its own subject
knowledge, using the questions PDF as visual context. One structured call returns a
:class:`KnowledgeScorecard` (one verdict per question) — no marks, no scores.

Unlike the rest of this vendored package, this module is specific to the dashboard's
homework feature and is intentionally NOT synced from the source notebook — keep it here.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from .core import _diagnose_empty, _normalize_qid, generate_with_retry
from .schemas import KnowledgeScorecard

_KNOWLEDGE_PROMPT = Path(__file__).resolve().parent / "prompts" / "knowledge_grade.txt"


def grade_submission_knowledge(
    client: genai.Client,
    *,
    subject: str,
    questions_images: list[Image.Image],
    answers: dict[str, str],
    subject_addendum: str = "",
    model: str = "gemini-3.5-flash",
    prompt_path: Path = _KNOWLEDGE_PROMPT,
    on_response: Callable[..., None] | None = None,
) -> KnowledgeScorecard:
    """Grade a submission WITHOUT a marking scheme, from the model's own knowledge.

    ``questions_images`` are the rendered pages of the questions PDF (the questions the
    student was asked). ``answers`` maps a question id to the student's answer text — the
    OCR transcript for handwritten submissions, or the raw typed answer for typed ones.
    ``subject_addendum`` is the per-course grading guidance (from ``course_configs``),
    appended when non-empty. Returns a :class:`KnowledgeScorecard` with one
    :class:`KnowledgeAnswerVerdict` per question found in the PDF.
    """
    prompt = Path(prompt_path).read_text(encoding="utf-8")

    contents: list = [prompt, f"\n# Subject\n{subject}\n"]
    if subject_addendum:
        contents.append(
            "\n# Subject-specific grading guidance\n" + subject_addendum.strip() + "\n"
        )

    contents.append("\n=== QUESTIONS PDF — the questions the student was asked ===\n")
    for i, img in enumerate(questions_images, start=1):
        contents.append(f"[Questions PDF page {i}/{len(questions_images)}]")
        contents.append(img)

    contents.append("\n=== STUDENT ANSWERS — labelled by question id ===\n")
    if answers:
        for qid, text in answers.items():
            contents.append(f"[Answer to question {qid}]\n{text}\n")
    else:
        contents.append("(The student submitted no answers.)\n")

    response = generate_with_retry(
        client,
        label="knowledge-grade",
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=KnowledgeScorecard,
            temperature=0,
        ),
        on_response=on_response,
    )

    parsed = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini returned no parsed KnowledgeScorecard ({_diagnose_empty(response)}). "
            "Raw text:\n" + (response.text or "<empty>")
        )
    # Canonicalize qids so they line up with the OCR/typed answer keys.
    for verdict in parsed.answers:
        verdict.question_id = _normalize_qid(verdict.question_id)
    return parsed  # type: ignore[return-value]
