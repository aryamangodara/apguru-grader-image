"""Unit tests for the FRQ-only MCQ filter.

The grader grades free-response only: multiple-choice questions must never be graded,
appear on the scorecard, or count toward the denominator. These cover ``is_mcq_question``
detection, the non-destructive ``drop_mcq_questions`` rubric filter, and the ``_do_grade``
wiring that strips MCQs before grading (and fails loud on an all-MCQ rubric). DB + Gemini
are mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors import InvalidSubmissionError
from app.schemas.grader_schema import GradedScorecardResponse
from app.services import grader_job_service
from app.services.grader.schemas import ParsedRubric, QuestionRubric, RubricPoint
from app.services.grader_exam_service import drop_mcq_questions, is_mcq_question


def _q(qid: str, criteria: list[str], max_points: float | None = None) -> QuestionRubric:
    pts = [
        RubricPoint(point_id=f"{qid}-{i}", question_id=qid, point_value=1.0, criterion=c)
        for i, c in enumerate(criteria, 1)
    ]
    return QuestionRubric(
        question_id=qid,
        prompt_summary=f"stem {qid}",
        rubric_points=pts,
        max_points=max_points if max_points is not None else float(len(pts)),
    )


def _mcq(qid: str, letter: str = "C") -> QuestionRubric:
    return _q(qid, [f"Correct Answer: ({letter}) some option text"], max_points=1.0)


def _rubric(questions: list[QuestionRubric]) -> ParsedRubric:
    return ParsedRubric(
        subject="World History", year=0,
        total_points=sum(q.max_points for q in questions),
        questions=questions,
    )


# --- is_mcq_question ---------------------------------------------------------

@pytest.mark.parametrize("crit", [
    "Correct Answer: (C) Neoconfucianism",
    "correct answer: (b)",
    "Correct Answer - A",
    "CORRECT ANSWER: (D) The Crusades",
])
def test_is_mcq_true_for_answer_key_variants(crit):
    assert is_mcq_question(_q("196", [crit])) is True


def test_is_mcq_false_for_frq():
    assert is_mcq_question(_q("216a", ["Identifies a specific change that resulted..."])) is False


def test_is_mcq_false_for_multi_point_frq():
    q = _q("218", ["Identifies a strong thesis...", "Provides contextualization...", "Uses evidence..."])
    assert is_mcq_question(q) is False


def test_is_mcq_false_for_no_points():
    q = QuestionRubric(question_id="x", prompt_summary="s", rubric_points=[], max_points=0.0)
    assert is_mcq_question(q) is False


def test_is_mcq_false_when_not_all_points_match():
    # One answer-key point + one descriptive point -> NOT an MCQ (conservative: needs ALL).
    q = _q("mix", ["Correct Answer: (A) foo", "Explains the reasoning in depth"])
    assert is_mcq_question(q) is False


# --- drop_mcq_questions ------------------------------------------------------

def test_drop_mcq_keeps_frq_and_recomputes_total():
    rubric = _rubric([
        _mcq("196"), _mcq("197"),
        _q("218", ["thesis", "context", "evidence", "analysis"], max_points=4.0),
        _q("220", ["thesis", "context", "pov", "evidence", "outside", "complexity"], max_points=6.0),
    ])
    filtered, dropped = drop_mcq_questions(rubric)
    assert dropped == ["196", "197"]
    assert [q.question_id for q in filtered.questions] == ["218", "220"]
    assert filtered.total_points == 10.0
    # Non-destructive: the original rubric is left untouched.
    assert len(rubric.questions) == 4
    assert rubric.total_points == 12.0


def test_drop_mcq_noop_returns_same_rubric():
    rubric = _rubric([_q("216a", ["Identifies a change"]), _q("216b", ["Explains a continuity"])])
    filtered, dropped = drop_mcq_questions(rubric)
    assert dropped == []
    assert filtered is rubric  # unchanged object, no copy


def test_drop_mcq_all_mcq_leaves_empty():
    rubric = _rubric([_mcq("1"), _mcq("2"), _mcq("3")])
    filtered, dropped = drop_mcq_questions(rubric)
    assert dropped == ["1", "2", "3"]
    assert filtered.questions == []
    assert filtered.total_points == 0.0


# --- _do_grade wiring --------------------------------------------------------

async def _run_do_grade(rubric: ParsedRubric):
    """Drive _do_grade for a typed quiz with the given cached rubric; return the grade mock."""
    job = {"exam_id": 5, "is_handwritten": 0, "student_id": 7, "answers_json": "{}", "answers_pdf_url": None}
    exam = {
        "id": 5, "test_id": 861, "course_id": "30", "test_name": "Quiz #861",
        "assessment_type": "quiz", "marking_scheme_pdf_url": "https://x/ms.pdf", "is_handwritten": 0,
    }
    db = MagicMock()
    db.query_one = AsyncMock(side_effect=[job, exam])
    db.write = AsyncMock(return_value=1)

    scorecard = MagicMock()
    scorecard.review_flags = []
    grade_result = {
        "scorecard": scorecard, "submission": MagicMock(),
        "recovered_qids": [], "merged_parent_answers": {}, "missing_qids": [],
    }
    built = GradedScorecardResponse(
        test_id=861, subject="World History", generated_at="t",
        grading_mode="rubric", percentage=50.0, total_points_earned=8.0,
        total_points_possible=16.0, questions_graded=8, is_handwritten=False,
    )
    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(grader_job_service, "require_langfuse_active"),
        patch.object(grader_job_service, "set_trace_attributes"),
        patch.object(grader_job_service, "record_trace_output"),
        patch.object(
            grader_job_service, "get_course_config",
            new=AsyncMock(return_value={"course_name": "World History", "exam_body": "College Board"}),
        ),
        patch.object(grader_job_service, "get_grading_addendum", new=AsyncMock(return_value="")),
        patch.object(grader_job_service, "get_ocr_addendum", new=AsyncMock(return_value="")),
        patch.object(grader_job_service, "get_gemini_client", return_value=MagicMock()),
        patch.object(grader_job_service, "is_rubric_free", return_value=False),
        patch.object(grader_job_service, "get_cached_rubric", return_value=rubric),
        patch.object(
            grader_job_service, "_build_typed_submission",
            new=AsyncMock(
                return_value=(MagicMock(), [], grader_job_service.RemapResult(answers_by_major={}))
            ),
        ),
        patch.object(grader_job_service, "grade_submission", return_value=grade_result) as mock_grade,
        patch.object(grader_job_service, "build_scorecard_response", return_value=built),
        patch.object(grader_job_service, "_record_job_output"),
        patch.object(grader_job_service, "_attach_summaries", new=AsyncMock()),
    ):
        await grader_job_service._do_grade("jobk")
    return mock_grade


async def test_do_grade_filters_mcqs_before_grading():
    rubric = _rubric([
        _mcq("196"), _mcq("197"),
        _q("216a", ["Identifies a change"]),
        _q("218", ["thesis", "evidence"], max_points=4.0),
    ])
    mock_grade = await _run_do_grade(rubric)
    graded_rubric = mock_grade.call_args.kwargs["rubric"]
    assert [q.question_id for q in graded_rubric.questions] == ["216a", "218"]
    assert all(not is_mcq_question(q) for q in graded_rubric.questions)


async def test_do_grade_all_mcq_fails_loud():
    rubric = _rubric([_mcq("1"), _mcq("2")])
    with pytest.raises(InvalidSubmissionError, match="no free-response questions"):
        await _run_do_grade(rubric)
