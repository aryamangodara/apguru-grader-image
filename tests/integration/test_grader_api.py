"""Integration tests for the AP FRQ grader endpoints (test_id-keyed).

Covers the refactor's contract: every route works on ``test_id`` (never
``exam_id``), register takes the 6-field body, typed exams submit answers inline,
and a submission for an unregistered test fails fast with 404. The service layer
is mocked — these assert the HTTP surface, not grading.
"""
from unittest.mock import AsyncMock, patch

from app.core.errors import InvalidSubmissionError, TestNotRegisteredError
from app.schemas.grader_schema import (
    ExamSummary,
    GradingJobResponse,
    JobSummary,
    RegisterExamResponse,
)

REGISTER_PATH = "/api/v1/grader/register-exam"


def _register_response(**overrides) -> RegisterExamResponse:
    base = {
        "test_id": 555,
        "course_id": "14",
        "subject": "AP Biology",
        "test_name": "March 2024",
        "is_handwritten": False,
        "total_points": 10.0,
        "question_count": 4,
        "parse_warnings": [],
        "cached": False,
    }
    base.update(overrides)
    return RegisterExamResponse(**base)


async def test_register_exam_returns_test_id(client):
    with patch(
        "app.services.grader_exam_service.register_exam",
        new=AsyncMock(return_value=_register_response()),
    ):
        resp = await client.post(
            REGISTER_PATH,
            json={
                "test_id": 555,
                "course_id": "14",
                "test_name": "March 2024",
                "is_handwritten": False,
                "marking_scheme_pdf_url": "https://example.com/ms.pdf",
            },
        )
    assert resp.status_code == 201
    data = resp.json()
    assert data["test_id"] == 555
    assert data["test_name"] == "March 2024"
    assert "exam_id" not in data  # the identifier is test_id everywhere now


async def test_register_handwritten_requires_questions_pdf(client):
    # The model validator rejects handwritten without a questions PDF → 422.
    resp = await client.post(
        REGISTER_PATH,
        json={
            "test_id": 1,
            "course_id": "14",
            "test_name": "X",
            "is_handwritten": True,
            "marking_scheme_pdf_url": "https://example.com/ms.pdf",
        },
    )
    assert resp.status_code == 422


async def test_submission_unregistered_test_returns_404(client):
    with (
        patch(
            "app.services.grader_job_service.create_job",
            new=AsyncMock(side_effect=TestNotRegisteredError("test_id 999 is not registered")),
        ),
        patch("app.services.grader_job_service.run_grading_job", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/v1/grader/exams/999/submissions",
            json={"student_id": 1, "answers": {"1": "a"}},
        )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "TEST_NOT_REGISTERED"  # consumers branch on this
    assert "not registered" in body["detail"]


async def test_submission_typed_inline_enqueues(client):
    with (
        patch(
            "app.services.grader_job_service.create_job",
            new=AsyncMock(return_value="job123"),
        ) as mock_create,
        patch("app.services.grader_job_service.run_grading_job", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/v1/grader/exams/555/submissions",
            json={"student_id": 7, "answers": {"1": "a1", "2": "a2"}},
        )
    assert resp.status_code == 202
    assert resp.json()["job_id"] == "job123"
    # Controller forwards the path test_id (int) and the parsed body.
    args, _ = mock_create.call_args
    assert args[0] == 555
    assert args[1].answers == {"1": "a1", "2": "a2"}


async def test_submission_typed_missing_answers_returns_400(client):
    with (
        patch(
            "app.services.grader_job_service.create_job",
            new=AsyncMock(side_effect=InvalidSubmissionError("answers is required for typed exams")),
        ),
        patch("app.services.grader_job_service.run_grading_job", new=AsyncMock()),
    ):
        resp = await client.post(
            "/api/v1/grader/exams/555/submissions",
            json={"student_id": 7},
        )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "INVALID_SUBMISSION"


async def test_get_job_returns_test_id(client):
    job = GradingJobResponse(
        job_id="job123",
        test_id=555,
        student_id=7,
        status="queued",
        is_handwritten=False,
    )
    with patch(
        "app.services.grader_job_service.get_job",
        new=AsyncMock(return_value=job),
    ):
        resp = await client.get("/api/v1/grader/jobs/job123")
    assert resp.status_code == 200
    data = resp.json()
    assert data["test_id"] == 555
    assert "exam_id" not in data


async def test_list_exams_returns_test_id(client):
    summary = ExamSummary(
        test_id=555,
        course_id="14",
        subject="AP Biology",
        test_name="March 2024",
        is_handwritten=False,
    )
    with patch(
        "app.services.grader_exam_service.list_exams",
        new=AsyncMock(return_value=[summary]),
    ):
        resp = await client.get("/api/v1/grader/exams")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    assert data["exams"][0]["test_id"] == 555
    assert data["exams"][0]["test_name"] == "March 2024"


async def test_list_jobs_by_student_returns_summaries(client):
    job = JobSummary(
        job_id="job123",
        test_id=536,
        student_id=3139,
        status="succeeded",
        is_handwritten=True,
        percentage=42.86,
        test_name="AP Psychology",
    )
    with patch(
        "app.services.grader_job_service.list_jobs",
        new=AsyncMock(return_value=[job]),
    ):
        resp = await client.get("/api/v1/grader/jobs?student_id=3139")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 1
    item = data["jobs"][0]
    assert item["job_id"] == "job123"
    assert item["test_id"] == 536
    assert item["percentage"] == 42.86
    assert "scorecard" not in item  # summary view omits the full scorecard


async def test_list_jobs_by_test_id(client):
    with patch(
        "app.services.grader_job_service.list_jobs",
        new=AsyncMock(return_value=[]),
    ) as mock_list:
        resp = await client.get("/api/v1/grader/jobs?test_id=536")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
    # Controller forwards the query params by keyword (test_id only).
    _, kwargs = mock_list.call_args
    assert kwargs == {"student_id": None, "test_id": 536}


async def test_list_jobs_no_filter_returns_400(client):
    with patch(
        "app.services.grader_job_service.list_jobs",
        new=AsyncMock(),
    ) as mock_list:
        resp = await client.get("/api/v1/grader/jobs")
    assert resp.status_code == 400
    assert "at least one" in resp.json()["detail"]
    mock_list.assert_not_called()  # guard short-circuits before the service
