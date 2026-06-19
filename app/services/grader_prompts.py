"""Exam-body-aware prompt selection for the AP / IB / Cambridge grader.

The vendored grader engine (``app/services/grader``) ships AP-shaped prompts that
assume discrete, all-or-nothing rubric points. Other boards mark differently:

- **IB** — markband / level-descriptor subjects (History, Economics essays, Business
  Management, …) score each assessment criterion by the *level reached*, not
  independent points.
- **Cambridge / Edexcel IGCSE & A-Level** — the same dual shape: point-marked
  sciences / maths plus levels-of-response for essay subjects, with Cambridge
  mark-scheme conventions (M/A marks, ECF, ORA, OWTTE, CAO, levels of response).

To grade all three without changing the API schema or the vendored engine, we keep a
prompt-file set per board alongside the AP one and pick between them by the course's
``exam_body`` (resolved from ``course_configs``).

``register_exam`` and ``_do_grade`` already hold the course row, so selection is a
one-call branch at each site; the vendored ``parse_rubric_pdf`` / ``grade_submission``
already accept the prompt path as a parameter, so nothing in the engine changes. AP
courses (``exam_body`` "College Board") and any unrecognized body fall through to the
unchanged AP prompts.
"""
from __future__ import annotations

from pathlib import Path

from app.services.grader import GRADE_PROMPT, PROMPTS_DIR, RUBRIC_PROMPT

# IB-specific prompt variants (additive — the AP prompts are untouched).
RUBRIC_PROMPT_IB: Path = PROMPTS_DIR / "rubric_extract_ib.txt"
GRADE_PROMPT_IB: Path = PROMPTS_DIR / "grade_question_ib.txt"

# Cambridge / Edexcel IGCSE & A-Level prompt variants (additive).
RUBRIC_PROMPT_CAMBRIDGE: Path = PROMPTS_DIR / "rubric_extract_cambridge.txt"
GRADE_PROMPT_CAMBRIDGE: Path = PROMPTS_DIR / "grade_question_cambridge.txt"

# course_configs.exam_body values that flag a non-AP course (compared case- and
# whitespace-insensitively). These rows are seeded by the central
# apguru-centralized-alembic repo (IB: migration 028; Cambridge: migration 031).
IB_EXAM_BODY = "IBO"
CAMBRIDGE_EXAM_BODY = "CAMBRIDGE IGCSE/A-LEVEL"


def _normalize(exam_body: str | None) -> str:
    """Canonical comparison form for an ``exam_body`` (trimmed, upper-cased)."""
    return (exam_body or "").strip().upper()


def is_ib_exam_body(exam_body: str | None) -> bool:
    """True if this course's ``exam_body`` marks it as an IB exam (case-insensitive)."""
    return _normalize(exam_body) == IB_EXAM_BODY


def is_cambridge_exam_body(exam_body: str | None) -> bool:
    """True if ``exam_body`` marks it as a Cambridge IGCSE/A-Level exam (case-insensitive)."""
    return _normalize(exam_body) == CAMBRIDGE_EXAM_BODY


def rubric_prompt_for(exam_body: str | None) -> Path:
    """Rubric-extraction prompt for a course: IB / Cambridge variant, else AP."""
    eb = _normalize(exam_body)
    if eb == IB_EXAM_BODY:
        return RUBRIC_PROMPT_IB
    if eb == CAMBRIDGE_EXAM_BODY:
        return RUBRIC_PROMPT_CAMBRIDGE
    return RUBRIC_PROMPT


def grade_prompt_for(exam_body: str | None) -> Path:
    """Per-question grading prompt for a course: IB / Cambridge variant, else AP."""
    eb = _normalize(exam_body)
    if eb == IB_EXAM_BODY:
        return GRADE_PROMPT_IB
    if eb == CAMBRIDGE_EXAM_BODY:
        return GRADE_PROMPT_CAMBRIDGE
    return GRADE_PROMPT
