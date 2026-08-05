"""Router for the homework & quiz auto-grader endpoints (the /grader/assessments surface).

Routes (all under /api/v1/grader/assessments). The **assessment kind** (`homework` | `quiz`)
is a field in the request **body** (register/submit) or a **query** param (listings) — never in
the URL path:
  POST /register                    — register (body carries assessment_type + source_id)
  GET  /?assessment_type=           — list registered homework/quiz
  POST /{source_id}/submissions     — enqueue grading (body carries assessment_type)
  GET  /jobs?assessment_type=       — list jobs by student_id and/or source_id
  GET  /jobs/{job_id}               — poll job status / scorecard

This is the exam grader's engine (register → OCR/typed → grade → scorecard, polling-only) retargeted
at homework/quiz. It reuses the same services and tables, keyed on `(assessment_type, source_id)` where
`source_id` is the main-app source-table id (`docs_homework_test.id` / `quiz.id`). The exam/test
endpoints in ``grader_router`` are untouched.

Like the exam router, these endpoints are intentionally PUBLIC (no JWT) and MUST be restricted at the
edge; caller-supplied PDF URLs are SSRF-guarded in ``app/services/grader/url_guard.py``.

OpenAPI/docs convention (mirrors ``grader_router``): framework-raised codes (405, 422) are documented
router-wide in ``_ERROR_RESPONSES``; each route declares ONLY the domain codes it actually raises, with
concrete ``{error_code, detail}`` examples whose messages mirror what the services raise.
"""
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Body, Path, Query

from app.controllers import assessment_controller
from app.core.errors import ErrorCode, ErrorResponse
from app.schemas.assessment_schema import (
    AssessmentJobListResponse,
    AssessmentJobResponse,
    AssessmentListResponse,
    AssessmentRegistrationResponse,
    AssessmentSubmissionRequest,
    AssessmentType,
    RegisterAssessmentRequest,
)
from app.schemas.grader_schema import CreateSubmissionResponse


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


# Framework-raised codes documented on EVERY assessment route (attached at the router level, so
# every operation inherits the {error_code, detail} envelope). Domain codes are declared per-route.
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

# --- POST /register ----------------------------------------------------------
_REGISTER_ERROR_RESPONSES = {
    400: {
        **_coded_400(
            (
                "invalid_source_id",
                ErrorCode.INVALID_TEST_ID,
                "homework id 211 is not a valid homework (not found or deleted)",
                "source_id is not a live row in the source table",
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
        "description": "The source_id, course_id, or a supplied PDF URL was rejected.",
    }
}
_REGISTER_REQUEST_EXAMPLES = {
    "homework": {
        "summary": "Homework (handwritten — needs questions_pdf_url for OCR context)",
        "value": {
            "assessment_type": "homework",
            "source_id": 211,
            "course_id": "16",
            "title": "Chapter 5 Homework",
            "is_handwritten": True,
            "marking_scheme_pdf_url": "https://papervideo.s3.ap-south-1.amazonaws.com/apguru/assets/marking_scheme/hw-211.pdf",
            "questions_pdf_url": "https://papervideo.s3.ap-south-1.amazonaws.com/apguru/assets/homework/hw-211.pdf",
        },
    },
    "homework_no_marking_scheme": {
        "summary": "Homework WITHOUT a marking scheme (graded by AI knowledge — right/wrong, no marks)",
        "value": {
            "assessment_type": "homework",
            "source_id": 212,
            "course_id": "16",
            "title": "Chapter 6 Homework",
            "is_handwritten": False,
            "questions_pdf_url": "https://papervideo.s3.ap-south-1.amazonaws.com/apguru/assets/homework/hw-212.pdf",
        },
    },
    "quiz": {
        "summary": "Quiz (handwritten — needs questions_pdf_url for OCR context)",
        "value": {
            "assessment_type": "quiz",
            "source_id": 869,
            "course_id": "30",
            "title": "Unit 3 Quiz",
            "is_handwritten": True,
            "marking_scheme_pdf_url": "https://papervideo.s3.ap-south-1.amazonaws.com/apguru/assets/marking_scheme/quiz-869.pdf",
            "questions_pdf_url": "https://papervideo.s3.ap-south-1.amazonaws.com/apguru/assets/quiz/quiz-869.pdf",
        },
    },
}

# --- POST /{source_id}/submissions -------------------------------------------
_SUBMISSION_ERROR_RESPONSES = {
    400: {
        **_coded_400(
            (
                "typed_missing_answers",
                ErrorCode.INVALID_SUBMISSION,
                "answers is required for typed assessments",
                "Typed assessment: inline `answers` omitted",
            ),
            (
                "handwritten_missing_pdf",
                ErrorCode.INVALID_SUBMISSION,
                "answers_pdf_url is required for handwritten assessments",
                "Handwritten assessment: `answers_pdf_url` omitted",
            ),
        ),
        "description": "Submission body doesn't match the assessment's registered mode.",
    },
    404: {
        **_coded_400(
            (
                "assessment_not_registered",
                ErrorCode.TEST_NOT_REGISTERED,
                "homework id 211 is not registered",
                "No assessment registered for this source_id",
            ),
        ),
        "description": "No assessment is registered for this (assessment_type, source_id).",
    },
    409: {
        **_coded_400(
            (
                "rubric_not_generated",
                ErrorCode.RUBRIC_NOT_GENERATED,
                "homework id 211 is registered but its rubric is not generated yet",
                "Assessment registered but rubric not parsed yet",
            ),
        ),
        "description": "The assessment exists but its rubric hasn't been parsed yet.",
    },
}
_SUBMISSION_REQUEST_EXAMPLES = {
    "handwritten": {
        "summary": "Handwritten submission (answers PDF, OCR'd)",
        "value": {
            "assessment_type": "homework",
            "student_id": 1001,
            "answers_pdf_url": "https://papervideo.s3.ap-south-1.amazonaws.com/apguru/student/1001.pdf",
        },
    },
    "typed": {
        "summary": "Typed submission (inline answers, no OCR)",
        "value": {
            "assessment_type": "homework",
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
                "provide at least one of student_id or source_id",
                "Neither student_id nor source_id supplied",
            ),
        ),
        "description": "At least one of student_id / source_id is required.",
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

router = APIRouter(
    prefix="/grader/assessments",
    tags=["Assessments - Homework and Quizzes"],
    responses=_ERROR_RESPONSES,
)

# Reused param specs — assessment_type is a query filter on the listing routes.
_ASSESSMENT_TYPE_QUERY = Query(description="Which assessment kind to act on: 'homework' or 'quiz'.")


@router.post(
    "/register",
    response_model=AssessmentRegistrationResponse,
    status_code=201,
    summary="Register a homework/quiz & cache its rubric",
    responses=_REGISTER_ERROR_RESPONSES,
)
async def register_assessment(
    body: Annotated[RegisterAssessmentRequest, Body(openapi_examples=_REGISTER_REQUEST_EXAMPLES)],
) -> AssessmentRegistrationResponse:
    """Register a homework/quiz and parse + cache its rubric (idempotent — reused per student).

    The kind is the ``assessment_type`` field in the body (`homework` | `quiz`), and ``source_id`` is
    the main-app row id it grades. Same engine as the exam grader: parsing the marking scheme is a
    one-time Gemini call, and a repeat registration for the same ``(assessment_type, source_id)``
    returns the cached row without re-parsing. Handwritten assessments also need ``questions_pdf_url``.

    **Homework may be registered WITHOUT ``marking_scheme_pdf_url``** — it is then graded from the
    model's own subject knowledge (right/wrong per question with the correct answer, no marks; the
    scorecard comes back with ``grading_mode="knowledge"``). In that case ``questions_pdf_url`` is
    required (typed too), so the model can see each question. Quizzes still require a marking scheme.

    Errors (all rendered as the ``{error_code, detail}`` envelope; see the example responses):

    * **400 ``INVALID_TEST_ID``** — ``source_id`` isn't a live row in the source table
      (``docs_homework_test`` for homework, ``quiz`` for quiz).
    * **400 ``UNKNOWN_COURSE``** — ``course_id`` has no ``course_configs`` row.
    * **400 ``INVALID_PDF_URL``** — a supplied PDF URL is unfetchable / SSRF-blocked.
    * **422** — request validation (e.g. handwritten assessment missing ``questions_pdf_url``; a quiz,
      or homework graded without a marking scheme, missing the PDFs it needs).
    """
    return await assessment_controller.register_assessment(body)


@router.get(
    "",
    response_model=AssessmentListResponse,
    summary="List registered homework/quiz",
)
async def list_assessments(
    assessment_type: Annotated[AssessmentType, _ASSESSMENT_TYPE_QUERY],
    course_id: Annotated[
        str | None, Query(description="Filter to assessments in this course_configs.course_id.")
    ] = None,
) -> AssessmentListResponse:
    """List all registered assessments of ``?assessment_type=`` (newest first); optional ``?course_id=``.

    Never raises a domain error — an unknown ``course_id`` yields an empty list, not a 400.
    """
    return await assessment_controller.list_assessments(assessment_type, course_id)


@router.post(
    "/{source_id}/submissions",
    response_model=CreateSubmissionResponse,
    status_code=202,
    summary="Enqueue grading for a student",
    responses=_SUBMISSION_ERROR_RESPONSES,
)
async def create_submission(
    source_id: Annotated[
        int, Path(description="Source-table id (docs_homework_test.id / quiz.id) to grade against.")
    ],
    body: Annotated[AssessmentSubmissionRequest, Body(openapi_examples=_SUBMISSION_REQUEST_EXAMPLES)],
    background_tasks: BackgroundTasks,
) -> CreateSubmissionResponse:
    """Enqueue grading for one student submission; returns a job_id to poll.

    The kind is the ``assessment_type`` field in the body (it disambiguates ``source_id``, which can
    repeat across types). The mode is fixed at registration: a **handwritten** assessment requires
    ``answers_pdf_url``; a **typed** assessment requires inline ``answers``. Sending the field for the
    wrong mode is the usual cause of ``INVALID_SUBMISSION``.

    **Typed answer-key reconciliation.** For typed assessments the answer keys are
    reconciled with the rubric by content: if they don't match the rubric's question ids
    (a mis-keyed client, e.g. answers keyed ``"21".."24"`` for a rubric keyed
    ``"216a".."220"``), each answer is content-mapped onto the question(s) it answers and
    those questions come back flagged for review with an ``answer_mapping`` on the
    scorecard. If the answers can't be mapped confidently the job **fails** with
    ``error = "ANSWER_MAPPING_FAILED: …"`` instead of returning a misleading 0. These are
    async outcomes on GET /grader/assessments/jobs/{job_id}, not on this 202 response.

    Errors (all rendered as the ``{error_code, detail}`` envelope; see the example responses):

    * **400 ``INVALID_SUBMISSION``** — body doesn't match the assessment's mode.
    * **404 ``TEST_NOT_REGISTERED``** — no assessment registered for this ``(assessment_type, source_id)``.
    * **409 ``RUBRIC_NOT_GENERATED``** — assessment registered, rubric not parsed yet.
    """
    return await assessment_controller.create_submission(source_id, body, background_tasks)


@router.get(
    "/jobs",
    response_model=AssessmentJobListResponse,
    summary="List grading jobs",
    responses=_LIST_JOBS_ERROR_RESPONSES,
)
async def list_jobs(
    assessment_type: Annotated[AssessmentType, _ASSESSMENT_TYPE_QUERY],
    student_id: Annotated[
        int | None, Query(description="Filter to this student's jobs (>=1 of student_id/source_id required).")
    ] = None,
    source_id: Annotated[
        int | None,
        Query(description="Filter to jobs for this source_id (>=1 of student_id/source_id required)."),
    ] = None,
) -> AssessmentJobListResponse:
    """List grading jobs for ``?assessment_type=`` by ``?student_id=`` and/or ``?source_id=`` (newest first).

    At least one of student_id / source_id is required. Returns lightweight summaries — poll
    GET /grader/assessments/jobs/{job_id} for the full scorecard.

    * **400 ``MISSING_JOB_FILTER``** — neither ``student_id`` nor ``source_id`` supplied.
    """
    return await assessment_controller.list_jobs(assessment_type, student_id, source_id)


@router.get(
    "/jobs/{job_id}",
    response_model=AssessmentJobResponse,
    summary="Poll a grading job",
    responses=_GET_JOB_ERROR_RESPONSES,
)
async def get_job(
    job_id: Annotated[str, Path(description="The job_id returned when the submission was enqueued.")],
) -> AssessmentJobResponse:
    """Poll a grading job; the scorecard is present once status == 'succeeded'.

    Works for both homework and quiz jobs (the job_id is globally unique); the response echoes the
    ``assessment_type`` it belongs to.

    A typed job may **succeed** with an ``answer_mapping`` on the scorecard (and
    ``review_required=true``) when its answer keys had to be content-mapped onto the
    rubric, or **fail** with ``error = "ANSWER_MAPPING_FAILED: …"`` when they couldn't be
    mapped confidently.

    * **404 ``JOB_NOT_FOUND``** — no grading job matches ``job_id``.
    """
    return await assessment_controller.get_job(job_id)
