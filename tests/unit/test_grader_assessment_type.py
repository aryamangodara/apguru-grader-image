"""Unit tests for the assessment_type discriminator (homework & quiz grading).

The grader was generalized from exams to assessments: ``get_exam`` / ``assert_source_is_valid`` /
``list_exams`` / ``register_exam`` / ``create_job`` / ``list_jobs`` now carry an ``assessment_type``
(default ``"exam"``) that (a) selects the main-app source table validated against and (b) is part of
the ``ap_exam`` composite key. The DB is mocked at the ``query_one`` / ``query`` / ``write`` boundary —
these assert the dispatch + SQL, not real SQL execution.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors import InvalidTestError
from app.schemas.grader_schema import CreateSubmissionRequest, RegisterExamRequest
from app.services import grader_exam_service, grader_job_service

# --- assert_source_is_valid dispatches by assessment_type --------------------

@pytest.mark.parametrize(
    ("assessment_type", "expected_table"),
    [("homework", "FROM docs_homework_test"), ("quiz", "FROM quiz"), ("exam", "FROM tests")],
)
async def test_assert_source_is_valid_dispatches_to_source_table(assessment_type, expected_table):
    db = MagicMock()
    db.query_one = AsyncMock(return_value={"id": 42})
    with patch.object(grader_exam_service.Database, "get_instance", return_value=db):
        await grader_exam_service.assert_source_is_valid(42, assessment_type)  # must not raise
    sql, params = db.query_one.call_args.args
    assert expected_table in sql
    assert "deleted_at IS NULL" in sql
    assert params == {"id": 42}


async def test_assert_source_is_valid_rejects_missing_homework_with_typed_message():
    db = MagicMock()
    db.query_one = AsyncMock(return_value=None)
    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        pytest.raises(InvalidTestError, match="homework id 42 is not a valid homework"),
    ):
        await grader_exam_service.assert_source_is_valid(42, "homework")


# --- get_exam scopes by (assessment_type, test_id) ---------------------------

async def test_get_exam_filters_by_assessment_type():
    db = MagicMock()
    db.query_one = AsyncMock(return_value=None)
    with patch.object(grader_exam_service.Database, "get_instance", return_value=db):
        await grader_exam_service.get_exam(211, "homework")
    sql, params = db.query_one.call_args.args
    assert "assessment_type = :atype" in sql
    assert params == {"atype": "homework", "tid": 211}


# --- list_exams filters by assessment_type -----------------------------------

async def test_list_exams_filters_by_assessment_type():
    db = MagicMock()
    db.query = AsyncMock(return_value=[])
    with patch.object(grader_exam_service.Database, "get_instance", return_value=db):
        await grader_exam_service.list_exams(assessment_type="quiz")
    sql, params = db.query.call_args.args
    assert "assessment_type = :atype" in sql
    assert params["atype"] == "quiz"


# --- register_exam: homework dispatch + INSERT binds assessment_type ---------

def _homework_req() -> RegisterExamRequest:
    return RegisterExamRequest(
        test_id=211,
        course_id="16",
        test_name="Chapter 5 Homework",
        is_handwritten=False,  # typed → no questions_pdf required
        marking_scheme_pdf_url="https://example.com/ms.pdf",
    )


async def test_register_homework_rejects_invalid_source_before_parsing():
    db = MagicMock()
    db.query_one = AsyncMock(return_value=None)  # docs_homework_test lookup: not found
    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        patch.object(grader_exam_service, "get_gemini_client") as mock_client,
        patch.object(grader_exam_service, "fetch_pdf_to_tempfile", new=AsyncMock()) as mock_fetch,
        patch.object(grader_exam_service, "parse_rubric_pdf") as mock_parse,
        pytest.raises(InvalidTestError, match="homework id 211 is not a valid homework"),
    ):
        await grader_exam_service.register_exam(_homework_req(), "homework")
    # Validation dispatched to docs_homework_test, before any PDF fetch or Gemini call.
    sql, _ = db.query_one.call_args.args
    assert "FROM docs_homework_test" in sql
    mock_client.assert_not_called()
    mock_fetch.assert_not_called()
    mock_parse.assert_not_called()


async def test_register_homework_binds_assessment_type_in_insert():
    rubric = MagicMock()
    rubric.total_points = 10.0
    rubric.questions = []
    rubric.parse_warnings = []
    rubric.model_dump_json.return_value = "{}"

    db = MagicMock()
    db.query_one = AsyncMock(return_value={"id": 211})  # source validation passes
    db.write = AsyncMock(return_value=1)

    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        patch.object(grader_exam_service, "get_exam", new=AsyncMock(return_value=None)),  # cache miss
        patch.object(
            grader_exam_service,
            "get_course_config",
            new=AsyncMock(return_value={"course_name": "AP Biology", "exam_body": "College Board"}),
        ),
        patch.object(grader_exam_service, "get_gemini_client", return_value=MagicMock()),
        patch.object(
            grader_exam_service, "fetch_pdf_to_tempfile", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(grader_exam_service, "parse_rubric_pdf", new=MagicMock(return_value=rubric)),
    ):
        resp = await grader_exam_service.register_exam(_homework_req(), "homework")

    assert resp.test_id == 211
    sql, params = db.write.call_args.args
    assert "assessment_type" in sql  # column present in the INSERT
    assert params["assessment_type"] == "homework"


# --- create_job / job listing carry assessment_type --------------------------

async def test_create_job_passes_assessment_type_to_get_exam():
    db = MagicMock()
    db.write = AsyncMock(return_value=1)
    exam_row = {"id": 5, "is_handwritten": 0, "rubric_json": "{}"}
    get_exam_mock = AsyncMock(return_value=exam_row)
    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(grader_job_service, "get_exam", new=get_exam_mock),
    ):
        job_key = await grader_job_service.create_job(
            211, CreateSubmissionRequest(student_id=7, answers={"1": "a"}), "homework"
        )
    assert job_key
    get_exam_mock.assert_awaited_once_with(211, "homework")
    db.write.assert_awaited_once()


async def test_list_jobs_defaults_to_exam_filter():
    db = MagicMock()
    db.query = AsyncMock(return_value=[])
    with patch.object(grader_job_service.Database, "get_instance", return_value=db):
        await grader_job_service.list_jobs(student_id=1)
    sql, params = db.query.call_args.args
    assert "e.assessment_type = :atype" in sql
    assert params["atype"] == "exam"


async def test_list_assessment_jobs_filters_by_type_and_source():
    db = MagicMock()
    db.query = AsyncMock(return_value=[])
    with patch.object(grader_job_service.Database, "get_instance", return_value=db):
        await grader_job_service.list_assessment_jobs("homework", source_id=211)
    sql, params = db.query.call_args.args
    assert "e.assessment_type = :atype" in sql
    assert params["atype"] == "homework"
    assert params["source_id"] == 211


async def test_get_assessment_job_excludes_exam_jobs():
    # An exam job_key must not resolve on the assessment surface: the lookup scopes to
    # homework/quiz, so an exam row is filtered out (-> None -> 404 JOB_NOT_FOUND) and
    # assessment_type='exam' is never coerced into the homework|quiz response model.
    db = MagicMock()
    db.query_one = AsyncMock(return_value=None)
    with patch.object(grader_job_service.Database, "get_instance", return_value=db):
        result = await grader_job_service.get_assessment_job("some-job-key")
    assert result is None
    sql, params = db.query_one.call_args.args
    assert "e.assessment_type IN ('homework', 'quiz')" in sql
    assert params == {"k": "some-job-key"}
