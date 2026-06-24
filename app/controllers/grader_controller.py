"""Thin controllers for the AP FRQ grader.

Orchestrate the services; domain failures raise typed ``GraderError``s (and the two
job-lookup/filter checks below) that the central handler in ``app/core/errors.py``
renders as the ``{error_code, detail}`` envelope — controllers no longer map errors
to HTTP themselves.
"""
from __future__ import annotations

from fastapi import BackgroundTasks

from app.core.errors import JobNotFoundError, MissingJobFilterError
from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    CreateSubmissionResponse,
    ExamListResponse,
    GradingJobResponse,
    JobListResponse,
    RegisterExamRequest,
    RegisterExamResponse,
)
from app.services import grader_exam_service, grader_job_service


async def register_exam(body: RegisterExamRequest) -> RegisterExamResponse:
    # Domain errors (InvalidTestError / UnknownCourseError / InvalidPdfUrlError) are
    # raised by the service and rendered by the central handler (app/core/errors.py).
    return await grader_exam_service.register_exam(body)


async def create_submission(
    test_id: int,
    body: CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    # TestNotRegisteredError (404) / RubricNotGeneratedError (409) /
    # InvalidSubmissionError (400) propagate to the central exception handler.
    job_key = await grader_job_service.create_job(test_id, body)
    background_tasks.add_task(grader_job_service.run_grading_job, job_key)
    return CreateSubmissionResponse(job_id=job_key, status="queued")


async def get_job(job_id: str) -> GradingJobResponse:
    job = await grader_job_service.get_job(job_id)
    if job is None:
        raise JobNotFoundError(f"unknown job_id {job_id!r}")
    return job


async def list_jobs(
    student_id: int | None = None, test_id: int | None = None
) -> JobListResponse:
    if student_id is None and test_id is None:
        raise MissingJobFilterError("provide at least one of student_id or test_id")
    jobs = await grader_job_service.list_jobs(student_id=student_id, test_id=test_id)
    return JobListResponse(count=len(jobs), jobs=jobs)


async def list_exams(course_id: str | None = None) -> ExamListResponse:
    exams = await grader_exam_service.list_exams(course_id)
    return ExamListResponse(count=len(exams), exams=exams)
