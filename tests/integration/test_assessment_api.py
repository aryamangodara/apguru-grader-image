"""Integration tests for the homework & quiz grader endpoints (/grader/assessments).

Covers the surface's HTTP contract: register/list/submit/poll keyed on
``(assessment_type, source_id)`` where ``assessment_type`` is a BODY field (register/submit) or a
QUERY param (listings) — never in the URL path — and that domain failures render the
``{error_code, detail}`` envelope. The service layer is mocked — these assert the HTTP surface and
the controller's schema mapping, not grading. The exam suite (test_grader_api.py) proves the exam
endpoints are untouched.
"""
from unittest.mock import AsyncMock, patch

from app.core.errors import InvalidSubmissionError, TestNotRegisteredError
from app.schemas.assessment_schema import AssessmentJobResponse, AssessmentJobSummary
from app.schemas.grader_schema import ExamSummary, RegisterExamResponse


def _exam_response(**overrides) -> RegisterExamResponse:
    base = {
        "test_id": 211,
        "course_id": "16",
        "subject": "AP Biology",
        "test_name": "Chapter 5 Homework",
        "is_handwritten": False,
        "total_points": 10.0,
        "question_count": 4,
        "parse_warnings": [],
        "cached": False,
    }
    base.update(overrides)
    return RegisterExamResponse(**base)


async def test_register_homework_maps_source_id_and_type(client):
    with patch(
        "app.services.grader_exam_service.register_exam",
        new=AsyncMock(return_value=_exam_response()),
    ) as mock_register:
        resp = await client.post(
            "/api/v1/grader/assessments/register",
            json={
                "assessment_type": "homework",
                "source_id": 211,
                "course_id": "16",
                "title": "Chapter 5 Homework",
                "is_handwritten": False,
                "marking_scheme_pdf_url": "https://example.com/ms.pdf",
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["assessment_type"] == "homework"
    assert data["source_id"] == 211
    assert data["title"] == "Chapter 5 Homework"
    assert "test_id" not in data  # the assessment surface speaks source_id
    # The service was called with assessment_type (from the body) forwarded as the 2nd arg.
    args, _ = mock_register.call_args
    assert args[1] == "homework"


async def test_register_handwritten_requires_questions_pdf(client):
    # RegisterAssessmentRequest's validator rejects handwritten without a questions PDF → 422.
    resp = await client.post(
        "/api/v1/grader/assessments/register",
        json={
            "assessment_type": "homework",
            "source_id": 1,
            "course_id": "16",
            "title": "X",
            "is_handwritten": True,
            "marking_scheme_pdf_url": "https://example.com/ms.pdf",
        },
    )
    assert resp.status_code == 422


async def test_unknown_assessment_type_is_rejected(client):
    # 'exam' (and anything outside homework|quiz) is not valid for the body enum → 422.
    resp = await client.post(
        "/api/v1/grader/assessments/register",
        json={
            "assessment_type": "exam",
            "source_id": 1,
            "course_id": "16",
            "title": "X",
            "is_handwritten": False,
            "marking_scheme_pdf_url": "https://example.com/ms.pdf",
        },
    )
    assert resp.status_code == 422


async def test_submission_forwards_assessment_type(client):
    with (
        patch(
            "app.services.grader_job_service.create_job", new=AsyncMock(return_value="jobABC")
        ) as mock_create,
        patch("app.services.grader_job_service.run_grading_job", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/v1/grader/assessments/211/submissions",
            json={
                "assessment_type": "homework",
                "student_id": 1001,
                "answers_pdf_url": "https://example.com/a.pdf",
            },
        )
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "jobABC"
    # Controller forwards (source_id, submission_req, assessment_type).
    args, _ = mock_create.call_args
    assert args[0] == 211
    assert args[2] == "homework"


async def test_submission_unregistered_source_returns_404(client):
    with (
        patch(
            "app.services.grader_job_service.create_job",
            new=AsyncMock(side_effect=TestNotRegisteredError("homework id 999 is not registered")),
        ),
        patch("app.services.grader_job_service.run_grading_job", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/v1/grader/assessments/999/submissions",
            json={"assessment_type": "homework", "student_id": 1, "answers": {"1": "a"}},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "TEST_NOT_REGISTERED"
    assert "not registered" in body["detail"]


async def test_submission_invalid_mode_returns_400(client):
    with (
        patch(
            "app.services.grader_job_service.create_job",
            new=AsyncMock(
                side_effect=InvalidSubmissionError("answers is required for typed assessments")
            ),
        ),
        patch("app.services.grader_job_service.run_grading_job", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/v1/grader/assessments/211/submissions",
            json={"assessment_type": "homework", "student_id": 7},
        )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_SUBMISSION"


async def test_list_jobs_no_filter_returns_400(client):
    with patch(
        "app.services.grader_job_service.list_assessment_jobs", new=AsyncMock()
    ) as mock_list:
        resp = await client.get("/api/v1/grader/assessments/jobs?assessment_type=homework")
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "MISSING_JOB_FILTER"
    mock_list.assert_not_called()  # guard short-circuits before the service


async def test_list_jobs_by_student_returns_summaries(client):
    job = AssessmentJobSummary(
        job_id="jobABC",
        assessment_type="homework",
        source_id=211,
        student_id=3139,
        status="succeeded",
        is_handwritten=True,
        percentage=88.5,
        title="Chapter 5 Homework",
    )
    with patch(
        "app.services.grader_job_service.list_assessment_jobs", new=AsyncMock(return_value=[job])
    ):
        resp = await client.get(
            "/api/v1/grader/assessments/jobs?assessment_type=homework&student_id=3139"
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    item = data["jobs"][0]
    assert item["source_id"] == 211
    assert item["assessment_type"] == "homework"
    assert item["percentage"] == 88.5
    assert "scorecard" not in item  # summary view omits the full scorecard


async def test_poll_job_returns_assessment_type(client):
    job = AssessmentJobResponse(
        job_id="jobABC",
        assessment_type="quiz",
        source_id=869,
        student_id=7,
        status="queued",
        is_handwritten=False,
    )
    with patch(
        "app.services.grader_job_service.get_assessment_job", new=AsyncMock(return_value=job)
    ):
        resp = await client.get("/api/v1/grader/assessments/jobs/jobABC")
    assert resp.status_code == 200
    data = resp.json()
    assert data["assessment_type"] == "quiz"
    assert data["source_id"] == 869


async def test_poll_unknown_job_returns_404(client):
    with patch(
        "app.services.grader_job_service.get_assessment_job", new=AsyncMock(return_value=None)
    ):
        resp = await client.get("/api/v1/grader/assessments/jobs/nope")
    assert resp.status_code == 404
    assert resp.json()["error_code"] == "JOB_NOT_FOUND"


async def test_list_assessments_maps_summaries(client):
    summary = ExamSummary(
        test_id=211,
        course_id="16",
        subject="AP Biology",
        test_name="Chapter 5 Homework",
        is_handwritten=False,
    )
    with patch(
        "app.services.grader_exam_service.list_exams", new=AsyncMock(return_value=[summary])
    ):
        resp = await client.get("/api/v1/grader/assessments?assessment_type=homework&course_id=16")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    item = data["assessments"][0]
    assert item["assessment_type"] == "homework"
    assert item["source_id"] == 211
    assert item["title"] == "Chapter 5 Homework"
