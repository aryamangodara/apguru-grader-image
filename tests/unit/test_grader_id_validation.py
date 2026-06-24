"""Unit tests for test_id / course_id validation on register + submission (issue #11).

Register-exam must reject a ``test_id`` that isn't a live row in the main app's
``tests`` table (non-existent or soft-deleted), and a submission must be refused
unless the exam is registered *with a generated rubric*. ``course_id`` existence is
already enforced by ``get_course_config`` (covered elsewhere). The DB is mocked at the
``query_one`` / ``get_exam`` boundary — these assert the guard logic, not real SQL.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors import InvalidTestError, RubricNotGeneratedError
from app.schemas.grader_schema import CreateSubmissionRequest, RegisterExamRequest
from app.services import grader_exam_service, grader_job_service

# --- assert_test_is_valid ----------------------------------------------------

async def test_assert_test_is_valid_rejects_unknown_or_deleted_test():
    db = MagicMock()
    db.query_one = AsyncMock(return_value=None)  # no live `tests` row
    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        pytest.raises(InvalidTestError, match="not a valid test"),
    ):
        await grader_exam_service.assert_test_is_valid(999)


async def test_assert_test_is_valid_accepts_live_test_and_filters_soft_deleted():
    db = MagicMock()
    db.query_one = AsyncMock(return_value={"id": 555})
    with patch.object(grader_exam_service.Database, "get_instance", return_value=db):
        await grader_exam_service.assert_test_is_valid(555)  # must not raise
    sql, params = db.query_one.call_args.args
    assert "FROM tests" in sql
    assert "deleted_at IS NULL" in sql  # valid == not soft-deleted
    assert params == {"test_id": 555}


# --- register_exam fails fast on an invalid test_id --------------------------

async def test_register_exam_rejects_invalid_test_id_before_parsing():
    req = RegisterExamRequest(
        test_id=999,
        course_id="14",
        test_name="X",
        is_handwritten=False,
        marking_scheme_pdf_url="https://example.com/ms.pdf",
    )
    db = MagicMock()
    db.query_one = AsyncMock(return_value=None)  # tests-table lookup: not found
    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        patch.object(grader_exam_service, "get_gemini_client") as mock_client,
        patch.object(grader_exam_service, "fetch_pdf_to_tempfile", new=AsyncMock()) as mock_fetch,
        patch.object(grader_exam_service, "parse_rubric_pdf") as mock_parse,
        pytest.raises(InvalidTestError, match="not a valid test"),
    ):
        await grader_exam_service.register_exam(req)
    # Fail-fast: the validation runs before any PDF fetch or Gemini call.
    mock_client.assert_not_called()
    mock_fetch.assert_not_called()
    mock_parse.assert_not_called()


# --- create_job requires a generated rubric ----------------------------------

def _typed_submission() -> CreateSubmissionRequest:
    return CreateSubmissionRequest(student_id=7, answers={"1": "a"})


async def test_create_job_rejects_when_rubric_not_generated():
    db = MagicMock()
    db.write = AsyncMock()
    exam_row = {"id": 1, "is_handwritten": 0, "rubric_json": None}
    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(grader_job_service, "get_exam", new=AsyncMock(return_value=exam_row)),
        pytest.raises(RubricNotGeneratedError, match="rubric is not generated"),
    ):
        await grader_job_service.create_job(1, _typed_submission())
    db.write.assert_not_called()  # nothing enqueued


async def test_create_job_accepts_when_rubric_present():
    db = MagicMock()
    db.write = AsyncMock(return_value=1)
    exam_row = {"id": 1, "is_handwritten": 0, "rubric_json": "{}"}
    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(grader_job_service, "get_exam", new=AsyncMock(return_value=exam_row)),
    ):
        job_key = await grader_job_service.create_job(1, _typed_submission())
    assert isinstance(job_key, str) and job_key
    db.write.assert_awaited_once()
