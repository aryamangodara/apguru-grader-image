"""Exam registration + cached-rubric access for the AP FRQ grader.

``register_exam`` parses the marking-scheme PDF with Gemini exactly once per exam
and stores the ParsedRubric JSON in ``ap_exam``; a repeat registration returns
the cached row without calling Gemini. ``get_cached_rubric`` rehydrates that JSON
for grading — never re-parsing.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import structlog

from app.core.config import settings
from app.core.course_config import get_course_config
from app.core.database import Database
from app.schemas.grader_schema import ExamSummary, RegisterExamRequest, RegisterExamResponse
from app.services.grader import (
    ParsedRubric,
    get_gemini_client,
    parse_rubric_pdf,
)
from app.services.grader.fetch import fetch_pdf_to_tempfile
from app.services.grader.tracing import gemini_generation_reporter
from app.services.grader_prompts import rubric_prompt_for

log = structlog.get_logger(__name__)


async def get_exam(test_id: int) -> dict | None:
    """Load an ap_exam row by the test_id it grades."""
    db = Database.get_instance()
    return await db.query_one(
        "SELECT * FROM ap_exam WHERE test_id = :tid AND deleted_at IS NULL",
        {"tid": test_id},
    )


def get_cached_rubric(exam_row: dict) -> ParsedRubric:
    """Rehydrate the cached ParsedRubric for an exam — no Gemini call."""
    return ParsedRubric.model_validate_json(exam_row["rubric_json"])


def _iso(value: Any) -> str | None:
    """Coerce a DB datetime to an ISO string (mirrors grader_job_service._iso)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


async def list_exams(course_id: str | None = None) -> list[ExamSummary]:
    """List registered exams (newest first), lightweight — never loads rubric_json.

    Optional ``course_id`` filters to a single course.
    """
    db = Database.get_instance()
    sql = (
        "SELECT test_id, course_id, test_name, is_handwritten, total_points, "
        "parse_warnings, questions_pdf_url, marking_scheme_pdf_url, rubric_parsed_at, created_at "
        "FROM ap_exam WHERE deleted_at IS NULL"
    )
    params: dict[str, Any] = {}
    if course_id:
        sql += " AND course_id = :course_id"
        params["course_id"] = course_id
    sql += " ORDER BY created_at DESC"
    rows = await db.query(sql, params)

    exams: list[ExamSummary] = []
    for row in rows:
        course = await get_course_config(row["course_id"])
        warnings = row["parse_warnings"]
        if isinstance(warnings, str):
            warnings = json.loads(warnings) if warnings else []
        exams.append(
            ExamSummary(
                test_id=row["test_id"],
                course_id=row["course_id"],
                subject=course.get("course_name") or row["course_id"],
                test_name=row["test_name"],
                is_handwritten=bool(row["is_handwritten"]),
                total_points=row["total_points"],
                parse_warnings=warnings or [],
                questions_pdf_url=row["questions_pdf_url"],
                marking_scheme_pdf_url=row["marking_scheme_pdf_url"],
                rubric_parsed_at=_iso(row["rubric_parsed_at"]),
                created_at=_iso(row["created_at"]),
            )
        )
    return exams


async def register_exam(req: RegisterExamRequest) -> RegisterExamResponse:
    """Register an exam for a test_id, parsing + caching its rubric once.

    Idempotent: a repeat registration for the same ``test_id`` returns the cached
    row without calling Gemini. To re-parse (e.g. a corrected marking scheme),
    delete the existing row first.
    """
    db = Database.get_instance()

    # Idempotent on test_id: the first registration wins, so a cache hit echoes
    # the stored row (course_id/test_name/subject all from what was persisted).
    existing = await get_exam(req.test_id)
    if existing:
        rubric = get_cached_rubric(existing)
        course = await get_course_config(existing["course_id"])
        log.info("grader_exam_cache_hit", test_id=req.test_id)
        return RegisterExamResponse(
            test_id=req.test_id,
            course_id=existing["course_id"],
            subject=course.get("course_name") or existing["course_id"],
            test_name=existing["test_name"],
            is_handwritten=bool(existing["is_handwritten"]),
            total_points=existing.get("total_points") or rubric.total_points,
            question_count=len(rubric.questions),
            parse_warnings=rubric.parse_warnings,
            cached=True,
        )

    course = await get_course_config(req.course_id)
    subject = course.get("course_name") or req.course_id

    # Parse the marking scheme once (offload the blocking Gemini call to a thread).
    # The vendored parse_rubric_pdf still takes year/set_label as LLM context: year
    # is unused post-refactor (pass 0); test_name flows in as the set label.
    client = get_gemini_client(prefer_vertex=settings.grader_use_vertex)
    pdf_path = await fetch_pdf_to_tempfile(req.marking_scheme_pdf_url)
    try:
        rubric = await asyncio.to_thread(
            parse_rubric_pdf,
            client,
            pdf_path,
            subject=subject,
            year=0,
            set_label=req.test_name,
            prompt_path=rubric_prompt_for(course.get("exam_body")),
            model=settings.grader_rubric_model,
            on_response=gemini_generation_reporter(
                "grader.rubric_parse", settings.grader_rubric_model
            ),
        )
    finally:
        pdf_path.unlink(missing_ok=True)

    # Upsert keyed on the unique test_id. We only reach here when no *active*
    # exam exists (an active one returns cached above), but a soft-deleted row
    # (deleted_at set) still occupies test_id under uq_ap_exam_test_id — so a
    # plain INSERT would hit a duplicate-key error on the documented
    # delete-then-re-register flow. ON DUPLICATE KEY UPDATE re-parses and
    # restores that row (deleted_at = NULL).
    await db.write(
        "INSERT INTO ap_exam (test_id, course_id, test_name, is_handwritten, rubric_json, "
        "questions_pdf_url, marking_scheme_pdf_url, total_points, parse_warnings, "
        "rubric_parsed_at) VALUES (:test_id, :course_id, :test_name, :is_handwritten, "
        ":rubric_json, :questions_pdf_url, :marking_scheme_pdf_url, :total_points, "
        ":parse_warnings, UTC_TIMESTAMP()) "
        "ON DUPLICATE KEY UPDATE course_id=:course_id, test_name=:test_name, "
        "is_handwritten=:is_handwritten, rubric_json=:rubric_json, "
        "questions_pdf_url=:questions_pdf_url, marking_scheme_pdf_url=:marking_scheme_pdf_url, "
        "total_points=:total_points, parse_warnings=:parse_warnings, "
        "rubric_parsed_at=UTC_TIMESTAMP(), deleted_at=NULL",
        {
            "test_id": req.test_id,
            "course_id": req.course_id,
            "test_name": req.test_name,
            "is_handwritten": req.is_handwritten,
            "rubric_json": rubric.model_dump_json(),
            "questions_pdf_url": req.questions_pdf_url,
            "marking_scheme_pdf_url": req.marking_scheme_pdf_url,
            "total_points": rubric.total_points,
            "parse_warnings": json.dumps(rubric.parse_warnings),
        },
    )

    log.info("grader_exam_registered", test_id=req.test_id, questions=len(rubric.questions))
    return RegisterExamResponse(
        test_id=req.test_id,
        course_id=req.course_id,
        subject=subject,
        test_name=req.test_name,
        is_handwritten=req.is_handwritten,
        total_points=rubric.total_points,
        question_count=len(rubric.questions),
        parse_warnings=rubric.parse_warnings,
        cached=False,
    )
