"""Router for the AP FRQ auto-grader endpoints.

Routes (all under /api/v1/grader):
  POST /grader/register-exam                — register an exam (parse rubric once)
  GET  /grader/exams                        — list all registered exams
  POST /grader/exams/{test_id}/submissions  — enqueue grading for a student
  GET  /grader/jobs                         — list jobs by student_id and/or test_id
  GET  /grader/jobs/{job_id}                — poll job status / scorecard

Every endpoint is keyed by ``test_id`` (the ``tests.id`` the exam grades).

These endpoints are intentionally PUBLIC — no JWT in any environment (per product
decision). ``student_id`` is supplied in the submission body and is NOT validated
against any token, so there is no in-app authorization: the grading surface and the
returned scorecards must be restricted at the edge (ALB / Nginx / WAF / security
group). The caller-supplied PDF URLs are SSRF-guarded in the fetch layer
(``app/services/grader/url_guard.py``).
"""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Path, Query

from app.controllers import grader_controller
from app.core.errors import ErrorCode, ErrorResponse
from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    CreateSubmissionResponse,
    ExamListResponse,
    GradingJobResponse,
    JobListResponse,
    RegisterExamRequest,
    RegisterExamResponse,
)


def _error_example(error_code: ErrorCode, detail: str, *, summary: str) -> dict:
    """One named Swagger example of the {error_code, detail} envelope (shown at /docs)."""
    return {"summary": summary, "value": {"error_code": error_code.value, "detail": detail}}


# Documents the {error_code, detail} error envelope on every grader route (see /docs).
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    405: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}

# Concrete per-failure examples for the submit endpoint, so Swagger shows WHICH
# error_code it emits and when — not just a generic ErrorResponse $ref. The detail
# strings mirror the exact messages raised by grader_job_service.create_job so the
# docs stay truthful to the behaviour. (405/422 keep the router-wide generic entry.)
_SUBMISSION_ERROR_RESPONSES = {
    400: {
        "model": ErrorResponse,
        "description": "Submission body doesn't match the exam's registered mode.",
        "content": {
            "application/json": {
                "examples": {
                    "typed_missing_answers": _error_example(
                        ErrorCode.INVALID_SUBMISSION,
                        "answers is required for typed exams",
                        summary="Typed exam: inline `answers` omitted",
                    ),
                    "handwritten_missing_pdf": _error_example(
                        ErrorCode.INVALID_SUBMISSION,
                        "answers_pdf_url is required for handwritten exams",
                        summary="Handwritten exam: `answers_pdf_url` omitted",
                    ),
                }
            }
        },
    },
    404: {
        "model": ErrorResponse,
        "description": "No exam is registered for this test_id.",
        "content": {
            "application/json": {
                "examples": {
                    "test_not_registered": _error_example(
                        ErrorCode.TEST_NOT_REGISTERED,
                        "test_id 322 is not registered",
                        summary="No exam registered for this test_id",
                    ),
                }
            }
        },
    },
    409: {
        "model": ErrorResponse,
        "description": "The exam exists but its rubric hasn't been parsed yet.",
        "content": {
            "application/json": {
                "examples": {
                    "rubric_not_generated": _error_example(
                        ErrorCode.RUBRIC_NOT_GENERATED,
                        "test_id 322 is registered but its rubric is not generated yet",
                        summary="Exam registered but rubric not parsed yet",
                    ),
                }
            }
        },
    },
}
router = APIRouter(prefix="/grader", tags=["Grader"], responses=_ERROR_RESPONSES)


@router.post(
    "/register-exam",
    response_model=RegisterExamResponse,
    status_code=201,
    summary="Register an exam & cache its rubric",
)
async def register_exam(body: RegisterExamRequest) -> RegisterExamResponse:
    """Register an exam and parse + cache its rubric (idempotent — reused per student)."""
    return await grader_controller.register_exam(body)


@router.get("/exams", response_model=ExamListResponse, summary="List registered exams")
async def list_exams(
    course_id: Annotated[
        str | None, Query(description="Filter to exams in this course_configs.course_id.")
    ] = None,
) -> ExamListResponse:
    """List all registered exams (newest first); optional ?course_id= filter."""
    return await grader_controller.list_exams(course_id)


@router.post(
    "/exams/{test_id}/submissions",
    response_model=CreateSubmissionResponse,
    status_code=202,
    summary="Enqueue grading for a student",
    responses=_SUBMISSION_ERROR_RESPONSES,
)
async def create_submission(
    test_id: Annotated[int, Path(description="tests.id of the registered exam to grade against.")],
    body: CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    """Enqueue grading for one student submission; returns a job_id to poll.

    The exam's mode is fixed at registration: a **handwritten** exam requires
    ``answers_pdf_url``; a **typed** exam requires inline ``answers``. Sending the
    field for the wrong mode is the usual cause of ``INVALID_SUBMISSION`` — the
    endpoint validates the body against the stored mode, it does not infer the mode
    from which field you send.

    Errors (all rendered as the ``{error_code, detail}`` envelope; see the example
    responses below):

    * **400 ``INVALID_SUBMISSION``** — body doesn't match the exam's mode.
    * **404 ``TEST_NOT_REGISTERED``** — no exam registered for this ``test_id``.
    * **409 ``RUBRIC_NOT_GENERATED``** — exam registered, rubric not parsed yet.
    """
    return await grader_controller.create_submission(test_id, body, background_tasks)


@router.get("/jobs", response_model=JobListResponse, summary="List grading jobs")
async def list_jobs(
    student_id: Annotated[
        int | None, Query(description="Filter to this student's jobs (>=1 of student_id/test_id required).")
    ] = None,
    test_id: Annotated[
        int | None, Query(description="Filter to jobs for this tests.id (>=1 of student_id/test_id required).")
    ] = None,
) -> JobListResponse:
    """List grading jobs by ?student_id= and/or ?test_id= (newest first).

    At least one filter is required. Returns lightweight summaries — poll
    GET /jobs/{job_id} for the full scorecard.
    """
    return await grader_controller.list_jobs(student_id, test_id)


@router.get("/jobs/{job_id}", response_model=GradingJobResponse, summary="Poll a grading job")
async def get_job(
    job_id: Annotated[str, Path(description="The job_id returned when the submission was enqueued.")],
) -> GradingJobResponse:
    """Poll a grading job; the scorecard is present once status == 'succeeded'."""
    return await grader_controller.get_job(job_id)
