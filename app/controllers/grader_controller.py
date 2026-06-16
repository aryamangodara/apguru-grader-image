"""Thin controllers for the AP FRQ grader — translate service errors to HTTP."""
from __future__ import annotations

from fastapi import BackgroundTasks, HTTPException

from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    CreateSubmissionResponse,
    ExamListResponse,
    GradingJobResponse,
    RegisterExamRequest,
    RegisterExamResponse,
)
from app.services import grader_exam_service, grader_job_service


async def register_exam(body: RegisterExamRequest) -> RegisterExamResponse:
    try:
        return await grader_exam_service.register_exam(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def create_submission(
    test_id: int,
    body: CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    try:
        job_key = await grader_job_service.create_job(test_id, body)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    background_tasks.add_task(grader_job_service.run_grading_job, job_key)
    return CreateSubmissionResponse(job_id=job_key, status="queued")


async def get_job(job_id: str) -> GradingJobResponse:
    job = await grader_job_service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id!r}")
    return job


async def list_exams(course_id: str | None = None) -> ExamListResponse:
    exams = await grader_exam_service.list_exams(course_id)
    return ExamListResponse(count=len(exams), exams=exams)
