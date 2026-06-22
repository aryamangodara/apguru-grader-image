"""Unit tests for post-grading audience summaries (issue #14).

Covers the compact scorecard projection, the single structured Gemini call (including
that the Langfuse ``on_response`` hook is forwarded — traceability must not regress), and
the best-effort ``_attach_summaries`` wiring in the job service (flag on/off + failure
isolation). The LLM is mocked — these assert the plumbing, not summary quality.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.schemas.grader_schema import GradedQuestion, GradedScorecardResponse
from app.services import grader_job_service, grader_summaries
from app.services.grader_summaries import (
    AudienceSummaries,
    build_summary_view,
    generate_audience_summaries,
)


def _response() -> GradedScorecardResponse:
    return GradedScorecardResponse(
        test_id=555,
        subject="AP Biology",
        test_name="March 2024",
        generated_at="2026-06-19T00:00:00",
        percentage=70.0,
        total_points_earned=7.0,
        total_points_possible=10.0,
        questions_graded=2,
        is_handwritten=False,
        questions=[
            GradedQuestion(
                question_id="1", comment="Strong on enzymes.",
                points_earned=4.0, points_possible=5.0, status="graded",
            ),
            GradedQuestion(
                question_id="2", comment="Missed the second mark.",
                points_earned=3.0, points_possible=5.0, status="graded",
            ),
        ],
    )


# --- compact view ------------------------------------------------------------

def test_build_summary_view_is_compact_and_has_comments():
    view = build_summary_view(_response())
    assert view["percentage"] == 70.0
    assert {q["question_id"] for q in view["questions"]} == {"1", "2"}
    assert any("enzymes" in q["comment"] for q in view["questions"])
    assert "points" not in view["questions"][0]  # per-point evidence is intentionally excluded


# --- the LLM call ------------------------------------------------------------

def test_generate_audience_summaries_returns_three_fields_and_forwards_tracing(monkeypatch):
    parsed = AudienceSummaries(
        student_summary="You did well.", teacher_summary="Solid grasp.", parent_summary="Good progress.",
    )
    mock_call = MagicMock(return_value=MagicMock(parsed=parsed))
    monkeypatch.setattr(grader_summaries, "generate_with_retry", mock_call)
    on_resp = MagicMock()

    out = generate_audience_summaries(
        MagicMock(),
        subject="AP Biology",
        exam_body="College Board",
        scorecard_view=build_summary_view(_response()),
        model="gemini-3.5-flash",
        on_response=on_resp,
    )

    assert out.student_summary and out.teacher_summary and out.parent_summary
    _, kwargs = mock_call.call_args
    assert kwargs["on_response"] is on_resp  # Langfuse hook forwarded — traceability guard
    assert kwargs["config"].response_schema is AudienceSummaries
    assert any("70" in str(c) for c in kwargs["contents"])  # scorecard data is in the prompt


def test_generate_audience_summaries_raises_on_empty(monkeypatch):
    empty = MagicMock(return_value=MagicMock(parsed=None, text=""))
    monkeypatch.setattr(grader_summaries, "generate_with_retry", empty)
    with pytest.raises(RuntimeError, match="no parsed AudienceSummaries"):
        generate_audience_summaries(MagicMock(), subject="x", exam_body=None, scorecard_view={})


# --- best-effort wiring in the job service -----------------------------------

async def test_attach_summaries_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(grader_job_service.settings, "grader_enable_summaries", False)
    called = MagicMock()
    monkeypatch.setattr(grader_job_service, "generate_audience_summaries", called)
    resp = _response()

    await grader_job_service._attach_summaries(resp, client=MagicMock(), subject="x", exam_body=None, job_key="j")

    called.assert_not_called()
    assert resp.student_summary == "" and resp.teacher_summary == "" and resp.parent_summary == ""


async def test_attach_summaries_sets_fields_when_enabled(monkeypatch):
    monkeypatch.setattr(grader_job_service.settings, "grader_enable_summaries", True)
    parsed = AudienceSummaries(student_summary="S", teacher_summary="T", parent_summary="P")
    monkeypatch.setattr(grader_job_service, "generate_audience_summaries", MagicMock(return_value=parsed))
    resp = _response()

    await grader_job_service._attach_summaries(
        resp, client=MagicMock(), subject="AP Biology", exam_body="College Board", job_key="j"
    )

    assert resp.student_summary == "S"
    assert resp.teacher_summary == "T"
    assert resp.parent_summary == "P"


async def test_attach_summaries_swallows_failure(monkeypatch):
    monkeypatch.setattr(grader_job_service.settings, "grader_enable_summaries", True)
    boom = MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(grader_job_service, "generate_audience_summaries", boom)
    resp = _response()

    # Must NOT raise — a summaries failure can't fail the grade.
    await grader_job_service._attach_summaries(resp, client=MagicMock(), subject="x", exam_body=None, job_key="j")

    assert resp.student_summary == ""  # failure left the fields empty
