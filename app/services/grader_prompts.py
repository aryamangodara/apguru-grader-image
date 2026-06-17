"""Exam-body-aware prompt selection for the AP/IB grader.

The vendored grader engine (``app/services/grader``) ships AP-shaped prompts that
assume discrete, all-or-nothing rubric points. IB marks differently — markband /
level-descriptor subjects (History, Economics essays, Business Management, …) score
each assessment criterion by the *level reached*, not independent points. To grade
both without changing the API schema or the vendored engine, we keep a second set
of IB prompt files alongside the AP ones and pick between them by the course's
``exam_body`` (resolved from ``course_configs``).

``register_exam`` and ``_do_grade`` already hold the course row, so selection is a
one-call branch at each site; the vendored ``parse_rubric_pdf`` / ``grade_submission``
already accept the prompt path as a parameter, so nothing in the engine changes.
AP courses (``exam_body`` "College Board") fall through to the unchanged AP prompts.
"""
from __future__ import annotations

from pathlib import Path

from app.services.grader import GRADE_PROMPT, PROMPTS_DIR, RUBRIC_PROMPT

# IB-specific prompt variants (additive — the AP prompts are untouched).
RUBRIC_PROMPT_IB: Path = PROMPTS_DIR / "rubric_extract_ib.txt"
GRADE_PROMPT_IB: Path = PROMPTS_DIR / "grade_question_ib.txt"

# course_configs.exam_body value that flags an IB course (migration 028 seeds it).
IB_EXAM_BODY = "IBO"


def is_ib_exam_body(exam_body: str | None) -> bool:
    """True if this course's ``exam_body`` marks it as an IB exam (case-insensitive)."""
    return (exam_body or "").strip().upper() == IB_EXAM_BODY


def rubric_prompt_for(exam_body: str | None) -> Path:
    """Rubric-extraction prompt for a course: the IB variant for IB, else AP."""
    return RUBRIC_PROMPT_IB if is_ib_exam_body(exam_body) else RUBRIC_PROMPT


def grade_prompt_for(exam_body: str | None) -> Path:
    """Per-question grading prompt for a course: the IB variant for IB, else AP."""
    return GRADE_PROMPT_IB if is_ib_exam_body(exam_body) else GRADE_PROMPT
