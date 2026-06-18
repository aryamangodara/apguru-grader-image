"""Grading-job lifecycle: create, poll, and the in-process background worker.

``create_job`` inserts a queued ``grading_job``; ``run_grading_job`` (scheduled
via FastAPI ``BackgroundTasks``) builds the submission (OCR for handwritten,
typed-answer labelling for typed), grades it against the cached rubric, and
stores the UI-complete scorecard JSON. A module-level semaphore caps concurrent
grades; ``reap_stale_jobs`` (startup hook) fails jobs orphaned by a restart.

The whole grade is wrapped in a Langfuse ``@observe`` trace; the blocking
OCR/labelling/grading runs in ``asyncio.to_thread`` so the event loop is free.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime
from typing import Any

import structlog
from langfuse import observe

from app.core.config import settings
from app.core.course_config import (
    get_course_config,
    get_grading_addendum,
    get_ocr_addendum,
)
from app.core.database import Database
from app.core.observability import record_trace_output, set_trace_attributes
from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    GradedScorecardResponse,
    GradingJobResponse,
)
from app.services.grader import (
    OCR_PROMPT,
    SEGMENT_TYPED_PROMPT,
    get_gemini_client,
    grade_submission,
    label_typed_answers,
    ocr_submission,
    render_pdf_to_images,
)
from app.services.grader.core import _normalize_qid
from app.services.grader.fetch import fetch_pdf_to_tempfile
from app.services.grader.response_builder import build_scorecard_response
from app.services.grader.schemas import Scorecard
from app.services.grader.tracing import gemini_generation_reporter
from app.services.grader_exam_service import get_cached_rubric, get_exam
from app.services.grader_prompts import grade_prompt_for

log = structlog.get_logger(__name__)

# Caps simultaneous in-flight grades (loop-agnostic until awaited).
_SEMAPHORE = asyncio.Semaphore(settings.grader_max_concurrent_jobs)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


# --- job creation + polling --------------------------------------------------

async def create_job(test_id: int, req: CreateSubmissionRequest) -> str:
    """Insert a queued grading_job for one student submission; return its job_key.

    Fails with ``LookupError`` (→ 404) when no exam is registered for ``test_id``.
    """
    db = Database.get_instance()
    exam = await get_exam(test_id)
    if exam is None:
        raise LookupError(f"test_id {test_id} is not registered")

    is_handwritten = bool(exam["is_handwritten"])
    if is_handwritten and not req.answers_pdf_url:
        raise ValueError("answers_pdf_url is required for handwritten exams")
    if not is_handwritten and not req.answers:
        raise ValueError("answers is required for typed exams")

    job_key = uuid.uuid4().hex
    await db.write(
        "INSERT INTO grading_job (job_key, exam_id, student_id, is_handwritten, answers_pdf_url, "
        "answers_json, status) VALUES (:job_key, :exam_id, :student_id, "
        ":is_handwritten, :answers_pdf_url, :answers_json, 'queued')",
        {
            "job_key": job_key,
            "exam_id": exam["id"],
            "student_id": req.student_id,
            "is_handwritten": is_handwritten,
            "answers_pdf_url": req.answers_pdf_url,
            "answers_json": json.dumps(req.answers) if req.answers else None,
        },
    )
    log.info(
        "grader_job_created",
        job_key=job_key,
        test_id=test_id,
        mode="handwritten" if is_handwritten else "typed",
    )
    return job_key


async def get_job(job_id: str) -> GradingJobResponse | None:
    """Load a job by its public job_key, hydrating the scorecard when ready."""
    db = Database.get_instance()
    row = await db.query_one(
        "SELECT j.*, e.test_id FROM grading_job j JOIN ap_exam e ON e.id = j.exam_id "
        "WHERE j.job_key = :k",
        {"k": job_id},
    )
    if row is None:
        return None

    scorecard = None
    if row.get("scorecard_json"):
        scorecard = GradedScorecardResponse.model_validate_json(row["scorecard_json"])

    return GradingJobResponse(
        job_id=row["job_key"],
        test_id=row["test_id"],
        student_id=row["student_id"],
        status=row["status"],
        is_handwritten=bool(row["is_handwritten"]),
        review_required=bool(row["review_required"]),
        created_at=_iso(row.get("created_at")),
        started_at=_iso(row.get("started_at")),
        finished_at=_iso(row.get("finished_at")),
        scorecard=scorecard,
        error=row.get("error_message"),
    )


# --- the worker --------------------------------------------------------------

@observe(name="grader.job")
async def run_grading_job(job_key: str) -> None:
    """Background worker: grade one submission and store the result.

    Acquires the concurrency semaphore (so queued jobs wait without flipping to
    running), marks the job running, grades, and writes the scorecard. Any error
    marks the job failed with the message — it never raises into the event loop.
    """
    db = Database.get_instance()
    async with _SEMAPHORE:
        await db.write(
            "UPDATE grading_job SET status='running', started_at=UTC_TIMESTAMP() "
            "WHERE job_key=:k",
            {"k": job_key},
        )
        try:
            await _do_grade(job_key)
        except Exception as exc:
            log.exception("grader_job_failed", job_key=job_key)
            await db.write(
                "UPDATE grading_job SET status='failed', error_message=:e, "
                "finished_at=UTC_TIMESTAMP() WHERE job_key=:k",
                {"k": job_key, "e": str(exc)[:2000]},
            )


async def _do_grade(job_key: str) -> None:
    db = Database.get_instance()
    job = await db.query_one("SELECT * FROM grading_job WHERE job_key=:k", {"k": job_key})
    exam = await db.query_one("SELECT * FROM ap_exam WHERE id=:id", {"id": job["exam_id"]})

    rubric = get_cached_rubric(exam)
    course_id = exam["course_id"]
    course = await get_course_config(course_id)
    subject = course.get("course_name") or course_id
    grading_addendum = await get_grading_addendum(course_id)
    ocr_addendum = await get_ocr_addendum(course_id)
    is_handwritten = bool(job["is_handwritten"])

    set_trace_attributes(
        user_id=str(job["student_id"]),
        tags=["grader", "handwritten" if is_handwritten else "typed", str(course_id)],
        metadata={
            "test_id": exam["test_id"],
            "test_name": exam["test_name"],
            "course_id": course_id,
            "subject": subject,
            "job_key": job_key,
            "ocr_model": settings.grader_ocr_model,
            "ocr_thinking_level": settings.grader_ocr_thinking_level,
            "grading_model": settings.grader_grading_model,
            "rubric_model": settings.grader_rubric_model,
            "grading_max_workers": settings.grader_grading_max_workers,
        },
    )

    client = get_gemini_client(prefer_vertex=settings.grader_use_vertex)
    answers_pdf_url = job.get("answers_pdf_url")
    page_count: int | None = None
    ai_labelled: list[str] = []

    if is_handwritten:
        submission, page_count = await _build_handwritten_submission(client, exam, job, ocr_addendum)
    else:
        submission, ai_labelled = await _build_typed_submission(client, exam, job, rubric)

    result = await asyncio.to_thread(
        grade_submission,
        client,
        subject=subject,
        year=0,
        set_label=exam["test_name"],
        submission=submission,
        rubric=rubric,
        grade_prompt_path=grade_prompt_for(course.get("exam_body")),
        subject_addendum=grading_addendum,
        model_grading=settings.grader_grading_model,
        grading_max_workers=settings.grader_grading_max_workers,
        low_confidence_threshold=settings.grader_low_confidence_threshold,
        force_review_qids=set(ai_labelled) or None,
        on_response=gemini_generation_reporter("grader.grade", settings.grader_grading_model),
    )

    scorecard = result["scorecard"]
    response = build_scorecard_response(
        scorecard,
        rubric,
        result["submission"],
        test_id=exam["test_id"],
        test_name=exam["test_name"],
        is_handwritten=is_handwritten,
        recovered_qids=result["recovered_qids"],
        merged_parent_answers=result["merged_parent_answers"],
        missing_qids=result["missing_qids"],
        ai_labelled_qids=ai_labelled,
        low_confidence_threshold=settings.grader_low_confidence_threshold,
        answers_pdf_url=answers_pdf_url,
        page_count=page_count,
    )
    _record_job_output(scorecard)

    await db.write(
        "UPDATE grading_job SET status='succeeded', scorecard_json=:s, review_required=:r, "
        "finished_at=UTC_TIMESTAMP() WHERE job_key=:k",
        {
            "k": job_key,
            "s": response.model_dump_json(),
            "r": 1 if scorecard.review_flags else 0,
        },
    )
    log.info("grader_job_succeeded", job_key=job_key, percentage=scorecard.percentage)


async def _build_handwritten_submission(client, exam, job, ocr_addendum):
    answers_url = job["answers_pdf_url"]
    questions_url = exam.get("questions_pdf_url")
    if not questions_url:
        raise ValueError("exam has no questions_pdf_url for handwritten OCR context")

    ans_path = await fetch_pdf_to_tempfile(answers_url)
    q_path = await fetch_pdf_to_tempfile(questions_url)
    try:
        return await asyncio.to_thread(_ocr_blocking, client, q_path, ans_path, ocr_addendum)
    finally:
        ans_path.unlink(missing_ok=True)
        q_path.unlink(missing_ok=True)


def _ocr_blocking(client, q_path, ans_path, ocr_addendum):
    """Blocking: render both PDFs and OCR the answers (runs in a worker thread)."""
    q_imgs = render_pdf_to_images(q_path, dpi=settings.grader_ocr_dpi)
    a_imgs = render_pdf_to_images(ans_path, dpi=settings.grader_ocr_dpi)
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
    return submission, len(a_imgs)


async def _build_typed_submission(client, exam, job, rubric):
    """Build the submission for a typed exam from inline answers — no OCR, no DB fetch.

    ``answers_json`` is the submission's ``{major_question_id: answer_text}`` dict;
    keys are normalized to the rubric's canonical form before labelling.
    """
    raw = job.get("answers_json")
    answers = json.loads(raw) if isinstance(raw, str) else (raw or {})
    answers_by_major = {_normalize_qid(str(qid)): text for qid, text in answers.items()}

    return await asyncio.to_thread(
        label_typed_answers,
        client,
        answers_by_major=answers_by_major,
        rubric=rubric,
        prompt_path=SEGMENT_TYPED_PROMPT,
        model=settings.grader_typed_label_model,
        on_response=gemini_generation_reporter(
            "grader.typed_label", settings.grader_typed_label_model
        ),
    )


def _record_job_output(scorecard: Scorecard) -> None:
    """Record the graded scorecard summary as the grader.job trace output."""
    out: dict[str, Any] = {
        "percentage": scorecard.percentage,
        "total_points_earned": scorecard.total_points_earned,
        "total_points_possible": scorecard.total_points_possible,
        "questions_graded": len(scorecard.questions),
        "review_flag_count": len(scorecard.review_flags),
    }
    if scorecard.questions:
        out["question_scores"] = {
            q.question_id: {"earned": q.points_earned, "possible": q.points_possible}
            for q in scorecard.questions
        }
    record_trace_output(out)


# --- durability --------------------------------------------------------------

async def reap_stale_jobs() -> int:
    """Fail jobs stuck 'running' past the stale threshold (orphaned by a restart)."""
    db = Database.get_instance()
    n = await db.write(
        "UPDATE grading_job SET status='failed', "
        "error_message='job orphaned by a worker restart', finished_at=UTC_TIMESTAMP() "
        "WHERE status='running' "
        "AND started_at < DATE_SUB(UTC_TIMESTAMP(), INTERVAL :m MINUTE)",
        {"m": settings.grader_job_reaper_stale_minutes},
    )
    if n:
        log.warning("grader_reaped_stale_jobs", count=n)
    return n
