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

OpenAPI/docs convention: framework-raised codes (405 wrong method, 422 request
validation) are documented router-wide in ``_ERROR_RESPONSES``; each route then
declares ONLY the domain codes it actually raises, with concrete ``{error_code,
detail}`` examples whose messages mirror what the services raise — so /docs maps each
code to its real trigger instead of showing a blanket 4xx set on every endpoint.
"""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Path, Query

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


def _coded_400(*examples: tuple[str, ErrorCode, str, str]) -> dict:
    """A 400/404/409 response documenting the envelope + one named example per failure.

    Each ``examples`` entry is ``(key, error_code, detail, summary)``.
    """
    return {
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "examples": {
                    key: _error_example(code, detail, summary=summary)
                    for key, code, detail, summary in examples
                }
            }
        },
    }


# Framework-raised codes documented on EVERY grader route. Both carry a concrete example
# so Swagger doesn't auto-fill a misleading placeholder (it would otherwise show the
# first enum value, TEST_NOT_REGISTERED, + "string"). Domain codes (400/404/409) are NOT
# here — each route declares only the ones it actually raises (below).
_ERROR_RESPONSES = {
    405: {
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {
                    "error_code": ErrorCode.METHOD_NOT_ALLOWED.value,
                    "detail": "Method Not Allowed",
                }
            }
        },
    },
    422: {
        "model": ErrorResponse,
        "description": "Request validation failed — `detail` is FastAPI's field-error list.",
        "content": {
            "application/json": {
                "example": {
                    "error_code": ErrorCode.VALIDATION_ERROR.value,
                    "detail": [
                        {
                            "type": "missing",
                            "loc": ["body", "student_id"],
                            "msg": "Field required",
                            "input": {},
                        }
                    ],
                }
            }
        },
    },
}

# --- POST /register-exam -----------------------------------------------------
_REGISTER_ERROR_RESPONSES = {
    400: {
        **_coded_400(
            (
                "invalid_test_id",
                ErrorCode.INVALID_TEST_ID,
                "test_id 322 is not a valid test (not found or deleted)",
                "test_id is not a live row in the tests table",
            ),
            (
                "unknown_course",
                ErrorCode.UNKNOWN_COURSE,
                "Unknown course_id: 999",
                "course_id has no course_configs row",
            ),
            (
                "invalid_pdf_url",
                ErrorCode.INVALID_PDF_URL,
                "host 'files.internal' resolves to a non-public address (10.0.0.5)",
                "a PDF URL is unfetchable / SSRF-blocked",
            ),
        ),
        "description": "The test_id, course_id, or a supplied PDF URL was rejected.",
    }
}
_REGISTER_REQUEST_EXAMPLES = {
    "handwritten": {
        "summary": "Handwritten exam (needs questions_pdf_url for OCR context)",
        "value": {
            "test_id": 322,
            "course_id": "14",
            "test_name": "March 2024 Set 1",
            "is_handwritten": True,
            "marking_scheme_pdf_url": "https://files.example.com/ms/ap-bio-322.pdf",
            "questions_pdf_url": "https://files.example.com/q/ap-bio-322.pdf",
        },
    },
    "typed": {
        "summary": "Typed exam (questions_pdf_url not needed)",
        "value": {
            "test_id": 401,
            "course_id": "17",
            "test_name": "2024 Practice Set",
            "is_handwritten": False,
            "marking_scheme_pdf_url": "https://files.example.com/ms/ap-psych-401.pdf",
        },
    },
}

# --- POST /exams/{test_id}/submissions ---------------------------------------
# Concrete per-failure examples so Swagger shows WHICH error_code the submit endpoint
# emits and when. The detail strings mirror the exact messages raised by
# grader_job_service.create_job so the docs stay truthful to the behaviour.
_SUBMISSION_ERROR_RESPONSES = {
    400: {
        **_coded_400(
            (
                "typed_missing_answers",
                ErrorCode.INVALID_SUBMISSION,
                "answers is required for typed exams",
                "Typed exam: inline `answers` omitted",
            ),
            (
                "handwritten_missing_pdf",
                ErrorCode.INVALID_SUBMISSION,
                "answers_pdf_url is required for handwritten exams",
                "Handwritten exam: `answers_pdf_url` omitted",
            ),
        ),
        "description": "Submission body doesn't match the exam's registered mode.",
    },
    404: {
        **_coded_400(
            (
                "test_not_registered",
                ErrorCode.TEST_NOT_REGISTERED,
                "test_id 322 is not registered",
                "No exam registered for this test_id",
            ),
        ),
        "description": "No exam is registered for this test_id.",
    },
    409: {
        **_coded_400(
            (
                "rubric_not_generated",
                ErrorCode.RUBRIC_NOT_GENERATED,
                "test_id 322 is registered but its rubric is not generated yet",
                "Exam registered but rubric not parsed yet",
            ),
        ),
        "description": "The exam exists but its rubric hasn't been parsed yet.",
    },
}
# Named request-body examples so /docs shows BOTH submission shapes with a
# Handwritten/Typed switcher. A schema-level `examples` array only renders its first
# item, so the typed inline-`answers` shape would otherwise be invisible in the UI.
_SUBMISSION_REQUEST_EXAMPLES = {
    "handwritten": {
        "summary": "Handwritten submission (answers PDF, OCR'd)",
        "value": {
            "student_id": 1001,
            "answers_pdf_url": "https://files.example.com/answers/1001.pdf",
        },
    },
    "typed": {
        "summary": "Typed submission (inline answers, no OCR)",
        "value": {
            "student_id": 1001,
            "answers": {"1": "Mitochondria are the...", "2": "The independent variable is..."},
        },
    },
}

# --- GET /jobs ---------------------------------------------------------------
_LIST_JOBS_ERROR_RESPONSES = {
    400: {
        **_coded_400(
            (
                "missing_job_filter",
                ErrorCode.MISSING_JOB_FILTER,
                "provide at least one of student_id or test_id",
                "Neither student_id nor test_id supplied",
            ),
        ),
        "description": "At least one of student_id / test_id is required.",
    }
}

# --- GET /jobs/{job_id} ------------------------------------------------------
_GET_JOB_ERROR_RESPONSES = {
    404: {
        **_coded_400(
            (
                "job_not_found",
                ErrorCode.JOB_NOT_FOUND,
                "unknown job_id 'b3f1c2a4d5e6f7089a1b2c3d4e5f6071'",
                "job_id matches no grading job",
            ),
        ),
        "description": "No grading job matches this job_id.",
    }
}

router = APIRouter(prefix="/grader", tags=["Grader"], responses=_ERROR_RESPONSES)


@router.post(
    "/register-exam",
    response_model=RegisterExamResponse,
    status_code=201,
    summary="Register an exam & cache its rubric",
    responses=_REGISTER_ERROR_RESPONSES,
)
async def register_exam(
    body: Annotated[RegisterExamRequest, Body(openapi_examples=_REGISTER_REQUEST_EXAMPLES)],
) -> RegisterExamResponse:
    """Register an exam and parse + cache its rubric (idempotent — reused per student).

    Handwritten exams also need ``questions_pdf_url`` (OCR context); typed exams don't
    (see the Handwritten/Typed request examples). Parsing the marking scheme is a
    one-time Gemini call — a repeat registration for the same ``test_id`` returns the
    cached row without re-parsing.

    Errors (all rendered as the ``{error_code, detail}`` envelope; see the example
    responses below):

    * **400 ``INVALID_TEST_ID``** — ``test_id`` isn't a live row in the ``tests`` table.
    * **400 ``UNKNOWN_COURSE``** — ``course_id`` has no ``course_configs`` row.
    * **400 ``INVALID_PDF_URL``** — a supplied PDF URL is unfetchable / SSRF-blocked.
    * **422** — request validation (e.g. handwritten exam missing ``questions_pdf_url``).
    """
    return await grader_controller.register_exam(body)


@router.get("/exams", response_model=ExamListResponse, summary="List registered exams")
async def list_exams(
    course_id: Annotated[
        str | None, Query(description="Filter to exams in this course_configs.course_id.")
    ] = None,
) -> ExamListResponse:
    """List all registered exams (newest first); optional ?course_id= filter.

    Never raises a domain error — an unknown ``course_id`` yields an empty list, not a 400.
    """
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
    body: Annotated[
        CreateSubmissionRequest, Body(openapi_examples=_SUBMISSION_REQUEST_EXAMPLES)
    ],
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    """Enqueue grading for one student submission; returns a job_id to poll.

    The exam's mode is fixed at registration: a **handwritten** exam requires
    ``answers_pdf_url``; a **typed** exam requires inline ``answers`` (see the
    Handwritten/Typed request examples). Sending the field for the wrong mode is the
    usual cause of ``INVALID_SUBMISSION`` — the endpoint validates the body against the
    stored mode, it does not infer the mode from which field you send.

    Errors (all rendered as the ``{error_code, detail}`` envelope; see the example
    responses below):

    * **400 ``INVALID_SUBMISSION``** — body doesn't match the exam's mode.
    * **404 ``TEST_NOT_REGISTERED``** — no exam registered for this ``test_id``.
    * **409 ``RUBRIC_NOT_GENERATED``** — exam registered, rubric not parsed yet.
    """
    return await grader_controller.create_submission(test_id, body, background_tasks)


@router.get("/jobs", response_model=JobListResponse, summary="List grading jobs", responses=_LIST_JOBS_ERROR_RESPONSES)
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

    * **400 ``MISSING_JOB_FILTER``** — neither ``student_id`` nor ``test_id`` supplied.
    """
    return await grader_controller.list_jobs(student_id, test_id)


@router.get(
    "/jobs/{job_id}",
    response_model=GradingJobResponse,
    summary="Poll a grading job",
    responses=_GET_JOB_ERROR_RESPONSES,
)
async def get_job(
    job_id: Annotated[str, Path(description="The job_id returned when the submission was enqueued.")],
) -> GradingJobResponse:
    """Poll a grading job; the scorecard is present once status == 'succeeded'.

    * **404 ``JOB_NOT_FOUND``** — no grading job matches ``job_id``.
    """
    return await grader_controller.get_job(job_id)
