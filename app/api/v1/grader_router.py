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
from fastapi import APIRouter, BackgroundTasks

from app.controllers import grader_controller
from app.core.errors import ErrorResponse
from app.schemas.grader_schema import (
    CreateSubmissionRequest,
    CreateSubmissionResponse,
    ExamListResponse,
    GradingJobResponse,
    JobListResponse,
    RegisterExamRequest,
    RegisterExamResponse,
)

# Documents the {error_code, detail} error envelope on every grader route (see /docs).
_ERROR_RESPONSES = {
    400: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}
router = APIRouter(prefix="/grader", tags=["Grader"], responses=_ERROR_RESPONSES)


@router.post("/register-exam", response_model=RegisterExamResponse, status_code=201)
async def register_exam(body: RegisterExamRequest) -> RegisterExamResponse:
    """Register an exam and parse + cache its rubric (idempotent — reused per student)."""
    return await grader_controller.register_exam(body)


@router.get("/exams", response_model=ExamListResponse)
async def list_exams(course_id: str | None = None) -> ExamListResponse:
    """List all registered exams (newest first); optional ?course_id= filter."""
    return await grader_controller.list_exams(course_id)


@router.post(
    "/exams/{test_id}/submissions",
    response_model=CreateSubmissionResponse,
    status_code=202,
)
async def create_submission(
    test_id: int,
    body: CreateSubmissionRequest,
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    """Enqueue grading for one student submission; returns a job_id to poll."""
    return await grader_controller.create_submission(test_id, body, background_tasks)


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    student_id: int | None = None, test_id: int | None = None
) -> JobListResponse:
    """List grading jobs by ?student_id= and/or ?test_id= (newest first).

    At least one filter is required. Returns lightweight summaries — poll
    GET /jobs/{job_id} for the full scorecard.
    """
    return await grader_controller.list_jobs(student_id, test_id)


@router.get("/jobs/{job_id}", response_model=GradingJobResponse)
async def get_job(job_id: str) -> GradingJobResponse:
    """Poll a grading job; the scorecard is present once status == 'succeeded'."""
    return await grader_controller.get_job(job_id)
