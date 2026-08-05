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
from app.core.errors import (
    GraderError,
    InvalidSubmissionError,
    RubricNotGeneratedError,
    TestNotRegisteredError,
)
from app.core.observability import (
    record_trace_output,
    require_langfuse_active,
    set_trace_attributes,
)
from app.schemas.assessment_schema import (
    AssessmentJobResponse,
    AssessmentJobSummary,
)
from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    GradedScorecardResponse,
    GradingJobResponse,
    JobSummary,
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
from app.services.grader_answer_map_service import RemapResult, remap_typed_answers
from app.services.grader_exam_service import (
    drop_mcq_questions,
    get_cached_rubric,
    get_exam,
    is_rubric_free,
)
from app.services.grader_knowledge_service import grade_knowledge_submission
from app.services.grader_prompts import grade_prompt_for
from app.services.grader_summaries import build_summary_view, generate_audience_summaries

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

async def create_job(test_id: int, req: CreateSubmissionRequest, assessment_type: str = "exam") -> str:
    """Insert a queued grading_job for one student submission; return its job_key.

    ``assessment_type`` defaults to ``"exam"`` (the exam path is unchanged); the
    ``/assessments`` surface passes ``"homework"`` / ``"quiz"`` and ``test_id`` is then
    the source-table id. Raises ``TestNotRegisteredError`` (404) /
    ``RubricNotGeneratedError`` (409) / ``InvalidSubmissionError`` (400), each rendered
    as ``{error_code, detail}``.
    """
    db = Database.get_instance()
    exam = await get_exam(test_id, assessment_type)
    # Preserve the exam surface's exact wording; homework/quiz get a type-specific one.
    field = "test_id" if assessment_type == "exam" else f"{assessment_type} id"
    if exam is None:
        raise TestNotRegisteredError(f"{field} {test_id} is not registered")
    # issue #11: the assessment must actually be registered *with a generated rubric*
    # before we accept a submission to grade against it.
    if not exam.get("rubric_json"):
        raise RubricNotGeneratedError(
            f"{field} {test_id} is registered but its rubric is not generated yet"
        )

    is_handwritten = bool(exam["is_handwritten"])
    # Keep the exam surface's exact wording ("... for handwritten exams"); the
    # homework/quiz surface reads "... assessments".
    noun = "exams" if assessment_type == "exam" else "assessments"
    if is_handwritten and not req.answers_pdf_url:
        raise InvalidSubmissionError(f"answers_pdf_url is required for handwritten {noun}")
    if not is_handwritten and not req.answers:
        raise InvalidSubmissionError(f"answers is required for typed {noun}")

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
        assessment_type=assessment_type,
        mode="handwritten" if is_handwritten else "typed",
    )
    return job_key


async def get_job(job_id: str) -> GradingJobResponse | None:
    """Load a job by its public job_key, hydrating the scorecard when ready."""
    db = Database.get_instance()
    row = await db.query_one(
        "SELECT j.*, e.test_id FROM grading_job j JOIN assessment_registry e ON e.id = j.exam_id "
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


async def list_jobs(
    student_id: int | None = None, test_id: int | None = None, assessment_type: str = "exam"
) -> list[JobSummary]:
    """List grading jobs filtered by student_id and/or test_id (newest first).

    Lightweight: returns summaries without the full scorecard — only the score
    percentage is extracted in SQL (so the large scorecard_json blob is never
    transferred). At least one filter should be supplied (the controller
    enforces this).
    """
    db = Database.get_instance()
    sql = (
        "SELECT j.job_key, j.student_id, j.status, j.is_handwritten, j.review_required, "
        "j.created_at, j.started_at, j.finished_at, j.error_message, "
        "e.test_id, e.test_name, "
        "CAST(JSON_EXTRACT(j.scorecard_json, '$.percentage') AS DECIMAL(5,2)) AS percentage "
        "FROM grading_job j JOIN assessment_registry e ON e.id = j.exam_id "
        "WHERE e.assessment_type = :atype"
    )
    params: dict[str, Any] = {"atype": assessment_type}
    if student_id is not None:
        sql += " AND j.student_id = :student_id"
        params["student_id"] = student_id
    if test_id is not None:
        sql += " AND e.test_id = :test_id"
        params["test_id"] = test_id
    sql += " ORDER BY j.created_at DESC"
    rows = await db.query(sql, params)

    return [
        JobSummary(
            job_id=row["job_key"],
            test_id=row["test_id"],
            student_id=row["student_id"],
            status=row["status"],
            is_handwritten=bool(row["is_handwritten"]),
            review_required=bool(row["review_required"]),
            percentage=float(row["percentage"]) if row["percentage"] is not None else None,
            test_name=row.get("test_name"),
            created_at=_iso(row.get("created_at")),
            started_at=_iso(row.get("started_at")),
            finished_at=_iso(row.get("finished_at")),
            error=row.get("error_message"),
        )
        for row in rows
    ]


# --- assessment (homework/quiz) polling --------------------------------------
# Mirror get_job / list_jobs but return the assessment-shaped envelopes
# (source_id + assessment_type). Kept separate so the exam surface's models and
# behaviour are untouched.

async def get_assessment_job(job_id: str) -> AssessmentJobResponse | None:
    """Load a homework/quiz job by its public job_key, hydrating the scorecard when ready.

    Scoped to assessment rows (``assessment_type IN ('homework','quiz')``): an *exam*
    job_key resolves to ``None`` here so the controller returns the documented 404
    ``JOB_NOT_FOUND`` instead of the surface leaking an exam job — and so an
    ``assessment_type='exam'`` row is never coerced into the ``homework|quiz``-only
    ``AssessmentJobResponse`` model (which would raise a response-validation error).
    """
    db = Database.get_instance()
    row = await db.query_one(
        "SELECT j.*, e.test_id, e.assessment_type FROM grading_job j "
        "JOIN assessment_registry e ON e.id = j.exam_id "
        "WHERE j.job_key = :k AND e.assessment_type IN ('homework', 'quiz')",
        {"k": job_id},
    )
    if row is None:
        return None

    scorecard = None
    if row.get("scorecard_json"):
        scorecard = GradedScorecardResponse.model_validate_json(row["scorecard_json"])

    return AssessmentJobResponse(
        job_id=row["job_key"],
        assessment_type=row["assessment_type"],
        source_id=row["test_id"],
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


async def list_assessment_jobs(
    assessment_type: str, student_id: int | None = None, source_id: int | None = None
) -> list[AssessmentJobSummary]:
    """List homework/quiz jobs of ``assessment_type`` by student_id and/or source_id (newest first).

    Lightweight: only the score percentage is extracted in SQL (the large scorecard_json
    blob is never transferred). At least one of student_id / source_id should be supplied
    (the controller enforces this).
    """
    db = Database.get_instance()
    # Extract the lightweight score fields from scorecard_json in SQL (never transfer the
    # big blob). 'knowledge'-mode homework has no marks — its percentage is a placeholder 0,
    # so we surface grading_mode + correct_count/questions_total and null the percentage below.
    sql = (
        "SELECT j.job_key, j.student_id, j.status, j.is_handwritten, j.review_required, "
        "j.created_at, j.started_at, j.finished_at, j.error_message, "
        "e.test_id, e.test_name, e.assessment_type, "
        "JSON_UNQUOTE(JSON_EXTRACT(j.scorecard_json, '$.grading_mode')) AS grading_mode, "
        "CAST(JSON_EXTRACT(j.scorecard_json, '$.correct_count') AS SIGNED) AS correct_count, "
        "CAST(JSON_EXTRACT(j.scorecard_json, '$.questions_total') AS SIGNED) AS questions_total, "
        "CAST(JSON_EXTRACT(j.scorecard_json, '$.percentage') AS DECIMAL(5,2)) AS percentage "
        "FROM grading_job j JOIN assessment_registry e ON e.id = j.exam_id "
        "WHERE e.assessment_type = :atype"
    )
    params: dict[str, Any] = {"atype": assessment_type}
    if student_id is not None:
        sql += " AND j.student_id = :student_id"
        params["student_id"] = student_id
    if source_id is not None:
        sql += " AND e.test_id = :source_id"
        params["source_id"] = source_id
    sql += " ORDER BY j.created_at DESC"
    rows = await db.query(sql, params)

    summaries: list[AssessmentJobSummary] = []
    for row in rows:
        is_knowledge = row.get("grading_mode") == "knowledge"
        summaries.append(
            AssessmentJobSummary(
                job_id=row["job_key"],
                assessment_type=row["assessment_type"],
                source_id=row["test_id"],
                student_id=row["student_id"],
                status=row["status"],
                is_handwritten=bool(row["is_handwritten"]),
                review_required=bool(row["review_required"]),
                # No marks in knowledge mode — omit the placeholder 0 percentage.
                percentage=(
                    None
                    if is_knowledge or row["percentage"] is None
                    else float(row["percentage"])
                ),
                grading_mode=row.get("grading_mode"),
                correct_count=(
                    int(row["correct_count"]) if row.get("correct_count") is not None else None
                ),
                questions_total=(
                    int(row["questions_total"]) if row.get("questions_total") is not None else None
                ),
                title=row.get("test_name"),
                created_at=_iso(row.get("created_at")),
                started_at=_iso(row.get("started_at")),
                finished_at=_iso(row.get("finished_at")),
                error=row.get("error_message"),
            )
        )
    return summaries


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
            # Prefix a typed GraderError with its stable code so the job's `error` field is
            # machine-readable (e.g. "ANSWER_MAPPING_FAILED: ..."), mirroring the
            # {error_code, detail} envelope that synchronous endpoints return.
            message = (
                f"{exc.error_code.value}: {exc.message}"
                if isinstance(exc, GraderError)
                else str(exc)
            )
            await db.write(
                "UPDATE grading_job SET status='failed', error_message=:e, "
                "finished_at=UTC_TIMESTAMP() WHERE job_key=:k",
                {"k": job_key, "e": message[:2000]},
            )


async def _attach_summaries(
    response: GradedScorecardResponse,
    *,
    client: Any,
    subject: str,
    exam_body: str | None,
    job_key: str,
) -> None:
    """Generate and attach the three audience summaries (issue #14), best-effort.

    Gated by ``settings.grader_enable_summaries``. The summaries are additive — a
    failure is logged and leaves the fields empty but never fails the grade (the
    scorecard is the primary deliverable). The blocking Gemini call is offloaded to a
    thread and traced in Langfuse via ``gemini_generation_reporter`` (one
    ``grader.summaries`` generation span, nested under the job's grade trace).
    """
    if not settings.grader_enable_summaries:
        return
    try:
        summaries = await asyncio.to_thread(
            generate_audience_summaries,
            client,
            subject=subject,
            exam_body=exam_body,
            scorecard_view=build_summary_view(response),
            model=settings.grader_summaries_model,
            on_response=gemini_generation_reporter(
                "grader.summaries", settings.grader_summaries_model
            ),
        )
        response.student_summary = summaries.student_summary
        response.teacher_summary = summaries.teacher_summary
        response.parent_summary = summaries.parent_summary
    except Exception as exc:  # additive feature — never fail the grade on summaries
        log.warning("grader_summaries_failed", job_key=job_key, error=str(exc))


async def _do_grade(job_key: str) -> None:
    # Langfuse is mandatory — never run a grade (OCR + per-question grading +
    # summaries, all LLM calls) untraced. Fails the job with a clear message.
    require_langfuse_active()
    db = Database.get_instance()
    job = await db.query_one("SELECT * FROM grading_job WHERE job_key=:k", {"k": job_key})
    exam = await db.query_one("SELECT * FROM assessment_registry WHERE id=:id", {"id": job["exam_id"]})

    course_id = exam["course_id"]
    course = await get_course_config(course_id)
    subject = course.get("course_name") or course_id
    grading_addendum = await get_grading_addendum(course_id)
    ocr_addendum = await get_ocr_addendum(course_id)
    is_handwritten = bool(job["is_handwritten"])

    set_trace_attributes(
        user_id=str(job["student_id"]),
        tags=[
            "grader",
            str(exam["assessment_type"]),
            "handwritten" if is_handwritten else "typed",
            str(course_id),
        ],
        metadata={
            "test_id": exam["test_id"],
            "assessment_type": exam["assessment_type"],
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

    # Rubric-free homework (registered without a marking scheme): grade from the model's
    # own knowledge — right/wrong per question, no marks. Bypasses the rubric grade path
    # (grade_submission / build_scorecard_response / summaries) entirely.
    if is_rubric_free(exam):
        response, review_required = await grade_knowledge_submission(
            client,
            exam=exam,
            job=job,
            subject=subject,
            grading_addendum=grading_addendum,
            ocr_addendum=ocr_addendum,
        )
        _record_knowledge_output(response)
        await db.write(
            "UPDATE grading_job SET status='succeeded', scorecard_json=:s, review_required=:r, "
            "finished_at=UTC_TIMESTAMP() WHERE job_key=:k",
            {"k": job_key, "s": response.model_dump_json(), "r": 1 if review_required else 0},
        )
        log.info(
            "grader_job_succeeded",
            job_key=job_key,
            grading_mode="knowledge",
            correct_count=response.correct_count,
            questions_total=response.questions_total,
        )
        return

    rubric = get_cached_rubric(exam)
    # Grader is FRQ-only: strip any multiple-choice questions before grading so they are
    # never scored, shown on the scorecard, counted in the denominator, or targeted by the
    # answer auto-map. Filters a copy — the stored rubric_json is untouched.
    rubric, dropped_mcq_qids = drop_mcq_questions(rubric)
    if dropped_mcq_qids:
        log.info(
            "grader_dropped_mcq_questions",
            job_key=job_key, count=len(dropped_mcq_qids), qids=dropped_mcq_qids,
        )
    if not rubric.questions:
        raise InvalidSubmissionError(
            "assessment has no free-response questions to grade "
            "(all questions are multiple-choice; the grader is FRQ-only)"
        )
    answers_pdf_url = job.get("answers_pdf_url")
    page_count: int | None = None
    ai_labelled: list[str] = []
    # Rubric qids whose answer was content-mapped from a mismatched key (typed only);
    # flag them for review, and record the applied mapping on the scorecard.
    remapped_qids: list[str] = []
    answer_mapping = None

    if is_handwritten:
        submission, page_count = await _build_handwritten_submission(client, exam, job, ocr_addendum)
    else:
        submission, ai_labelled, remap = await _build_typed_submission(client, exam, job, rubric)
        remapped_qids = remap.remapped_qids
        if remap.remapped:
            answer_mapping = remap.applied_map

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
        force_review_qids=set(ai_labelled) | set(remapped_qids) or None,
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
    # Surface the applied answer->question mapping (typed mis-keyed submissions only;
    # None on the normal path). The remapped questions are already review-flagged above.
    response.answer_mapping = answer_mapping
    _record_job_output(scorecard)

    # issue #14: attach best-effort, Langfuse-traced audience summaries to the scorecard.
    await _attach_summaries(
        response, client=client, subject=subject, exam_body=course.get("exam_body"), job_key=job_key
    )

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


async def _build_typed_submission(
    client, exam, job, rubric
) -> tuple[Any, list[str], RemapResult]:
    """Build the submission for a typed exam from inline answers — no OCR, no DB fetch.

    ``answers_json`` is the submission's ``{question_id: answer_text}`` dict. Keys are
    normalized, then reconciled with the rubric: when they don't match the rubric's
    question ids, ``remap_typed_answers`` content-maps each answer onto the question(s)
    it answers (or fails the job loudly). The reconciled dict is then labelled by
    sub-part as usual. Returns ``(submission, ai_labelled_qids, remap)``.
    """
    raw = job.get("answers_json")
    answers = json.loads(raw) if isinstance(raw, str) else (raw or {})
    answers_by_major = {_normalize_qid(str(qid)): text for qid, text in answers.items()}

    remap = await asyncio.to_thread(
        remap_typed_answers, client, answers_by_major=answers_by_major, rubric=rubric
    )

    submission, ai_labelled = await asyncio.to_thread(
        label_typed_answers,
        client,
        answers_by_major=remap.answers_by_major,
        rubric=rubric,
        prompt_path=SEGMENT_TYPED_PROMPT,
        model=settings.grader_typed_label_model,
        on_response=gemini_generation_reporter(
            "grader.typed_label", settings.grader_typed_label_model
        ),
    )
    return submission, ai_labelled, remap


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


def _record_knowledge_output(response: GradedScorecardResponse) -> None:
    """Record the rubric-free (knowledge) grade summary as the grader.job trace output."""
    record_trace_output(
        {
            "grading_mode": "knowledge",
            "correct_count": response.correct_count,
            "questions_total": response.questions_total,
            "questions_graded": response.questions_graded,
            "review_flag_count": len(response.review_flags),
            "verdicts": {q.question_id: q.verdict for q in response.questions},
        }
    )


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
