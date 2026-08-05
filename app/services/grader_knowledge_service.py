"""Rubric-free grading orchestration for homework registered WITHOUT a marking scheme.

When a homework has no marking scheme there is no rubric to grade against, so this
service grades from the model's own subject knowledge and returns a right/wrong-only
scorecard (no marks). It reuses the vendored pipeline primitives — PDF fetch/render,
handwriting OCR — then calls :func:`grade_submission_knowledge` (one Gemini call) and
composes the shared :class:`GradedScorecardResponse` with ``grading_mode="knowledge"``,
per-question verdicts + correct answers, and an "X of Y correct" tally.

Selected by :func:`app.services.grader_exam_service.is_rubric_free`; wired into the job
worker's ``_do_grade``. The exam/quiz rubric path is untouched.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import structlog

from app.core.config import settings
from app.schemas.grader_schema import GradedQuestion, GradedScorecardResponse
from app.services.grader import (
    KNOWLEDGE_GRADE_PROMPT,
    OCR_PROMPT,
    grade_submission_knowledge,
    ocr_submission,
    render_pdf_to_images,
)
from app.services.grader.core import _normalize_qid
from app.services.grader.fetch import fetch_pdf_to_tempfile
from app.services.grader.schemas import KnowledgeScorecard
from app.services.grader.tracing import gemini_generation_reporter

log = structlog.get_logger(__name__)


async def grade_knowledge_submission(
    client,
    *,
    exam: dict,
    job: dict,
    subject: str,
    grading_addendum: str = "",
    ocr_addendum: str = "",
) -> tuple[GradedScorecardResponse, bool]:
    """Grade a rubric-free homework submission from the model's own knowledge.

    Fetches + renders the questions PDF (always required in this mode), obtains the
    student's answers (OCR for handwritten, inline ``answers_json`` for typed), asks
    Gemini to judge each answer right/wrong, and composes the UI scorecard. Returns
    ``(response, review_required)`` — ``review_required`` is True when any answer was
    low-confidence (grading or OCR).
    """
    questions_url = exam.get("questions_pdf_url")
    if not questions_url:
        # Registration requires questions_pdf_url in this mode; guard anyway.
        raise ValueError("homework has no questions_pdf_url for knowledge grading")

    is_handwritten = bool(job["is_handwritten"])
    answers_pdf_url = job.get("answers_pdf_url")
    typed_answers = {} if is_handwritten else _typed_answers(job)

    q_path = await fetch_pdf_to_tempfile(questions_url)
    ans_path = None
    try:
        if is_handwritten:
            ans_path = await fetch_pdf_to_tempfile(answers_pdf_url)
        scorecard, confidences, page_count = await asyncio.to_thread(
            _knowledge_grade_blocking,
            client,
            q_path=q_path,
            ans_path=ans_path,
            typed_answers=typed_answers,
            is_handwritten=is_handwritten,
            subject=subject,
            grading_addendum=grading_addendum,
            ocr_addendum=ocr_addendum,
        )
    finally:
        q_path.unlink(missing_ok=True)
        if ans_path is not None:
            ans_path.unlink(missing_ok=True)

    return _build_knowledge_response(
        scorecard,
        exam=exam,
        subject=subject,
        is_handwritten=is_handwritten,
        answers_pdf_url=answers_pdf_url,
        page_count=page_count,
        confidences=confidences,
    )


def _typed_answers(job: dict) -> dict[str, str]:
    """Read the typed ``{question_id: answer_text}`` map off the job row, normalized."""
    raw = job.get("answers_json")
    answers = json.loads(raw) if isinstance(raw, str) else (raw or {})
    return {_normalize_qid(str(qid)): str(text) for qid, text in answers.items()}


def _knowledge_grade_blocking(
    client,
    *,
    q_path,
    ans_path,
    typed_answers: dict[str, str],
    is_handwritten: bool,
    subject: str,
    grading_addendum: str,
    ocr_addendum: str,
) -> tuple[KnowledgeScorecard, dict[str, float], int | None]:
    """Blocking: render the questions PDF (+ OCR the answers for handwritten), then grade.

    Runs in a worker thread. Returns ``(knowledge_scorecard, ocr_confidences, page_count)``
    where ``ocr_confidences`` maps question_id -> OCR confidence (empty for typed) and
    ``page_count`` is the number of answer pages (None for typed).
    """
    q_imgs = render_pdf_to_images(q_path, dpi=settings.grader_ocr_dpi)
    confidences: dict[str, float] = {}
    page_count: int | None = None

    if is_handwritten:
        a_imgs = render_pdf_to_images(ans_path, dpi=settings.grader_ocr_dpi)
        page_count = len(a_imgs)
        submission = ocr_submission(
            client,
            q_imgs,
            a_imgs,
            OCR_PROMPT,
            model=settings.grader_ocr_model,
            thinking_level=settings.grader_ocr_thinking_level,
            subject_addendum=ocr_addendum,
            on_response=gemini_generation_reporter("grader.ocr", settings.grader_ocr_model),
        )
        answers = {a.question_id: a.transcript for a in submission.answers}
        confidences = {a.question_id: a.confidence for a in submission.answers}
    else:
        answers = dict(typed_answers)

    scorecard = grade_submission_knowledge(
        client,
        subject=subject,
        questions_images=q_imgs,
        answers=answers,
        subject_addendum=grading_addendum,
        model=settings.grader_knowledge_model,
        prompt_path=KNOWLEDGE_GRADE_PROMPT,
        on_response=gemini_generation_reporter(
            "grader.knowledge_grade", settings.grader_knowledge_model
        ),
    )
    return scorecard, confidences, page_count


def _build_knowledge_response(
    scorecard: KnowledgeScorecard,
    *,
    exam: dict,
    subject: str,
    is_handwritten: bool,
    answers_pdf_url: str | None,
    page_count: int | None,
    confidences: dict[str, float],
) -> tuple[GradedScorecardResponse, bool]:
    """Compose the UI scorecard from the knowledge verdicts (marks zeroed)."""
    threshold = settings.grader_low_confidence_threshold
    graded: list[GradedQuestion] = []
    unattempted: list[GradedQuestion] = []
    review_flags: list[str] = []
    correct_count = 0

    for v in scorecard.answers:
        oc = confidences.get(v.question_id)
        if v.verdict == "not_attempted":
            unattempted.append(
                GradedQuestion(
                    question_id=v.question_id,
                    comment=v.explanation or "",
                    correct_answer=v.correct_answer or None,
                    points_earned=0.0,
                    points_possible=0.0,
                    status="unattempted",
                    ocr_confidence=oc,
                )
            )
            continue

        if v.verdict == "correct":
            correct_count += 1
        low = (v.confidence == "low") or (oc is not None and oc < threshold)
        graded.append(
            GradedQuestion(
                question_id=v.question_id,
                comment=v.explanation,
                verdict=v.verdict,
                correct_answer=v.correct_answer or None,
                points_earned=0.0,
                points_possible=0.0,
                status="graded",
                transcript=v.student_answer,
                ocr_confidence=oc,
                low_confidence=low,
            )
        )
        if v.confidence == "low":
            review_flags.append(f"Q{v.question_id}: low grading confidence")
        if oc is not None and oc < threshold:
            review_flags.append(f"Q{v.question_id}: OCR confidence below threshold")

    response = GradedScorecardResponse(
        test_id=exam["test_id"],
        subject=subject,
        test_name=exam["test_name"],
        generated_at=datetime.now(UTC).isoformat(),
        grading_mode="knowledge",
        percentage=0.0,
        total_points_earned=0.0,
        total_points_possible=0.0,
        question_wise_marks=[],
        correct_count=correct_count,
        questions_total=len(scorecard.answers),
        questions_graded=len(graded),
        review_flags=review_flags,
        is_handwritten=is_handwritten,
        answers_pdf_url=answers_pdf_url,
        page_count=page_count,
        questions=graded,
        unattempted=unattempted,
    )
    log.info(
        "grader_knowledge_graded",
        test_id=exam["test_id"],
        correct=correct_count,
        total=len(scorecard.answers),
    )
    return response, bool(review_flags)
