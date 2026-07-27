"""Thin controllers for the homework & quiz auto-grader (the /grader/assessments surface).

Orchestrate the SAME services as the exam grader, taking ``assessment_type`` from the request
**body** (register/submit) or **query** (listings) and mapping between the assessment-shaped schemas
(``source_id`` + ``assessment_type``) and the services' exam-shaped I/O. Domain failures raise typed
``GraderError``s (plus the two job-lookup/filter checks here) that the central handler in
``app/core/errors.py`` renders as the ``{error_code, detail}`` envelope — controllers never map errors
to HTTP themselves.
"""
from __future__ import annotations

from fastapi import BackgroundTasks

from app.core.errors import JobNotFoundError, MissingJobFilterError
from app.schemas.assessment_schema import (
    AssessmentJobListResponse,
    AssessmentJobResponse,
    AssessmentListResponse,
    AssessmentRegistrationResponse,
    AssessmentSubmissionRequest,
    AssessmentSummary,
    AssessmentType,
    RegisterAssessmentRequest,
)
from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    CreateSubmissionResponse,
    RegisterExamRequest,
)
from app.services import grader_exam_service, grader_job_service


async def register_assessment(body: RegisterAssessmentRequest) -> AssessmentRegistrationResponse:
    # Reuse the exam registration service verbatim — map the assessment request onto its
    # RegisterExamRequest (source_id → test_id, title → test_name). Domain errors
    # (InvalidTestError / UnknownCourseError / InvalidPdfUrlError) propagate to the handler.
    exam_req = RegisterExamRequest(
        test_id=body.source_id,
        course_id=body.course_id,
        test_name=body.title,
        is_handwritten=body.is_handwritten,
        marking_scheme_pdf_url=body.marking_scheme_pdf_url,
        questions_pdf_url=body.questions_pdf_url,
    )
    resp = await grader_exam_service.register_exam(exam_req, body.assessment_type)
    return AssessmentRegistrationResponse(
        assessment_type=body.assessment_type,
        source_id=resp.test_id,
        course_id=resp.course_id,
        subject=resp.subject,
        title=resp.test_name,
        is_handwritten=resp.is_handwritten,
        total_points=resp.total_points,
        question_count=resp.question_count,
        parse_warnings=resp.parse_warnings,
        cached=resp.cached,
    )


async def create_submission(
    source_id: int,
    body: AssessmentSubmissionRequest,
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    # TestNotRegisteredError (404) / RubricNotGeneratedError (409) /
    # InvalidSubmissionError (400) propagate to the central exception handler.
    sub_req = CreateSubmissionRequest(
        student_id=body.student_id,
        answers_pdf_url=body.answers_pdf_url,
        answers=body.answers,
    )
    job_key = await grader_job_service.create_job(source_id, sub_req, body.assessment_type)
    background_tasks.add_task(grader_job_service.run_grading_job, job_key)
    return CreateSubmissionResponse(job_id=job_key, status="queued")


async def get_job(job_id: str) -> AssessmentJobResponse:
    job = await grader_job_service.get_assessment_job(job_id)
    if job is None:
        raise JobNotFoundError(f"unknown job_id {job_id!r}")
    return job


async def list_jobs(
    assessment_type: AssessmentType,
    student_id: int | None = None,
    source_id: int | None = None,
) -> AssessmentJobListResponse:
    if student_id is None and source_id is None:
        raise MissingJobFilterError("provide at least one of student_id or source_id")
    jobs = await grader_job_service.list_assessment_jobs(
        assessment_type, student_id=student_id, source_id=source_id
    )
    return AssessmentJobListResponse(count=len(jobs), jobs=jobs)


async def list_assessments(
    assessment_type: AssessmentType, course_id: str | None = None
) -> AssessmentListResponse:
    exams = await grader_exam_service.list_exams(course_id, assessment_type)
    assessments = [
        AssessmentSummary(
            assessment_type=assessment_type,
            source_id=e.test_id,
            course_id=e.course_id,
            subject=e.subject,
            title=e.test_name,
            is_handwritten=e.is_handwritten,
            total_points=e.total_points,
            parse_warnings=e.parse_warnings,
            questions_pdf_url=e.questions_pdf_url,
            marking_scheme_pdf_url=e.marking_scheme_pdf_url,
            rubric_parsed_at=e.rubric_parsed_at,
            created_at=e.created_at,
        )
        for e in exams
    ]
    return AssessmentListResponse(count=len(assessments), assessments=assessments)
