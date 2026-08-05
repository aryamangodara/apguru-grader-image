"""Unit tests for rubric-free homework grading (graded from the model's own knowledge).

Homework may be registered WITHOUT a marking scheme; it is then graded right/wrong-only
(no marks). These cover: the request-schema validators, ``is_rubric_free`` detection, the
``register_exam`` rubric-free branch (no LLM work, empty rubric stored), the
``MARKING_SCHEME_REQUIRED`` guard for exams/quizzes, the ``grade_submission_knowledge``
primitive mapping, the ``GradedScorecardResponse`` composition, and the ``_do_grade`` route
that bypasses the rubric path. DB + Gemini are mocked — no real SQL/LLM runs.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.core.errors import MarkingSchemeRequiredError
from app.schemas.assessment_schema import RegisterAssessmentRequest
from app.schemas.grader_schema import GradedScorecardResponse, RegisterExamRequest
from app.services import grader_exam_service, grader_job_service
from app.services import grader_knowledge_service as ks
from app.services.grader import knowledge
from app.services.grader.schemas import KnowledgeAnswerVerdict, KnowledgeScorecard

# --- request-schema validators ----------------------------------------------

def _hw(**overrides) -> dict:
    base = {
        "assessment_type": "homework",
        "source_id": 212,
        "course_id": "16",
        "title": "Chapter 6 Homework",
        "is_handwritten": False,
    }
    base.update(overrides)
    return base


def test_homework_typed_without_scheme_needs_questions():
    # No marking scheme + no questions PDF → rejected (can't grade from knowledge blind).
    with pytest.raises(ValidationError, match="questions_pdf_url is required when no marking"):
        RegisterAssessmentRequest(**_hw())


def test_homework_typed_without_scheme_with_questions_ok():
    req = RegisterAssessmentRequest(**_hw(questions_pdf_url="https://x/q.pdf"))
    assert req.marking_scheme_pdf_url is None
    assert req.questions_pdf_url == "https://x/q.pdf"


def test_homework_handwritten_without_scheme_with_questions_ok():
    req = RegisterAssessmentRequest(**_hw(is_handwritten=True, questions_pdf_url="https://x/q.pdf"))
    assert req.marking_scheme_pdf_url is None


def test_quiz_without_scheme_rejected():
    with pytest.raises(ValidationError, match="marking_scheme_pdf_url is required for quizzes"):
        RegisterAssessmentRequest(
            **_hw(assessment_type="quiz", questions_pdf_url="https://x/q.pdf")
        )


# --- is_rubric_free detection ------------------------------------------------

@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"assessment_type": "homework", "marking_scheme_pdf_url": None}, True),
        ({"assessment_type": "homework", "marking_scheme_pdf_url": ""}, True),
        ({"assessment_type": "homework", "marking_scheme_pdf_url": "https://x/ms.pdf"}, False),
        ({"assessment_type": "quiz", "marking_scheme_pdf_url": None}, False),
        ({"assessment_type": "exam", "marking_scheme_pdf_url": None}, False),
    ],
)
def test_is_rubric_free(row, expected):
    assert grader_exam_service.is_rubric_free(row) is expected


# --- register_exam: rubric-free homework skips all LLM work -------------------

async def test_register_homework_without_marking_scheme_skips_parse():
    db = MagicMock()
    db.query_one = AsyncMock(return_value={"id": 212})  # source validation passes
    db.write = AsyncMock(return_value=1)
    req = RegisterExamRequest(
        test_id=212,
        course_id="16",
        test_name="Chapter 6 Homework",
        is_handwritten=False,
        questions_pdf_url="https://x/q.pdf",
    )
    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        patch.object(grader_exam_service, "get_exam", new=AsyncMock(return_value=None)),  # cache miss
        patch.object(
            grader_exam_service,
            "get_course_config",
            new=AsyncMock(return_value={"course_name": "AP Biology", "exam_body": "College Board"}),
        ),
        patch.object(grader_exam_service, "require_langfuse_active") as mock_lf,
        patch.object(grader_exam_service, "get_gemini_client") as mock_client,
        patch.object(grader_exam_service, "fetch_pdf_to_tempfile", new=AsyncMock()) as mock_fetch,
        patch.object(grader_exam_service, "parse_rubric_pdf") as mock_parse,
    ):
        resp = await grader_exam_service.register_exam(req, "homework")

    # Rubric-free registration makes NO Gemini call and no Langfuse guard.
    mock_lf.assert_not_called()
    mock_client.assert_not_called()
    mock_fetch.assert_not_called()
    mock_parse.assert_not_called()
    # An empty rubric is persisted, marking scheme is null, zero points.
    _, params = db.write.call_args.args
    assert params["marking_scheme_pdf_url"] is None
    assert params["total_points"] == 0.0
    stored = json.loads(params["rubric_json"])
    assert stored["questions"] == []
    assert stored["total_points"] == 0.0
    assert resp.total_points == 0.0
    assert resp.question_count == 0


async def test_register_exam_without_marking_scheme_raises():
    db = MagicMock()
    db.query_one = AsyncMock(return_value={"id": 322})  # source validation passes
    req = RegisterExamRequest(test_id=322, course_id="14", test_name="X", is_handwritten=False)
    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        patch.object(grader_exam_service, "get_exam", new=AsyncMock(return_value=None)),
        patch.object(
            grader_exam_service,
            "get_course_config",
            new=AsyncMock(return_value={"course_name": "AP Bio", "exam_body": "College Board"}),
        ),
        patch.object(grader_exam_service, "get_gemini_client") as mock_client,
        pytest.raises(MarkingSchemeRequiredError, match="required for exam registration"),
    ):
        await grader_exam_service.register_exam(req, "exam")
    mock_client.assert_not_called()  # rejected before any LLM setup


# --- grade_submission_knowledge primitive ------------------------------------

def test_grade_submission_knowledge_maps_and_normalizes_qids():
    resp = MagicMock()
    resp.parsed = KnowledgeScorecard(
        answers=[
            KnowledgeAnswerVerdict(
                question_id="1A",  # upper-case → must be normalized to "1a"
                verdict="correct",
                explanation="ok",
                correct_answer="42",
                student_answer="42",
                confidence="high",
            )
        ]
    )
    with patch.object(knowledge, "generate_with_retry", return_value=resp):
        out = knowledge.grade_submission_knowledge(
            MagicMock(), subject="Math", questions_images=[], answers={"1a": "42"}
        )
    assert isinstance(out, KnowledgeScorecard)
    assert out.answers[0].question_id == "1a"


# --- GradedScorecardResponse composition -------------------------------------

def _mixed_scorecard() -> KnowledgeScorecard:
    return KnowledgeScorecard(
        answers=[
            KnowledgeAnswerVerdict(
                question_id="1", verdict="correct", explanation="great",
                correct_answer="A", student_answer="A", confidence="high",
            ),
            KnowledgeAnswerVerdict(
                question_id="2", verdict="incorrect", explanation="nope",
                correct_answer="B", student_answer="C", confidence="high",
            ),
            KnowledgeAnswerVerdict(
                question_id="3", verdict="partial", explanation="half",
                correct_answer="D", student_answer="d", confidence="low",
            ),
            KnowledgeAnswerVerdict(
                question_id="4", verdict="not_attempted", explanation="",
                correct_answer="E", student_answer="", confidence="low",
            ),
        ]
    )


def test_build_knowledge_response_composition():
    response, review_required = ks._build_knowledge_response(
        _mixed_scorecard(),
        exam={"test_id": 212, "test_name": "Chapter 6 Homework"},
        subject="AP Biology",
        is_handwritten=False,
        answers_pdf_url=None,
        page_count=None,
        confidences={},
    )
    assert response.grading_mode == "knowledge"
    # No marks anywhere.
    assert response.percentage == 0.0
    assert response.total_points_earned == 0.0
    assert response.total_points_possible == 0.0
    assert response.question_wise_marks == []
    # "X of Y correct" — only "correct" verdicts count.
    assert response.correct_count == 1
    assert response.questions_total == 4
    assert response.questions_graded == 3
    assert len(response.questions) == 3
    assert len(response.unattempted) == 1
    q1 = next(q for q in response.questions if q.question_id == "1")
    assert q1.verdict == "correct"
    assert q1.correct_answer == "A"
    assert q1.comment == "great"
    # Low grading confidence on Q3 → a review flag → review_required.
    assert review_required is True
    assert any("Q3" in f for f in response.review_flags)
    unattempted = response.unattempted[0]
    assert unattempted.question_id == "4"
    assert unattempted.verdict is None
    assert unattempted.status == "unattempted"
    assert unattempted.correct_answer == "E"


def test_build_knowledge_response_flags_low_ocr_confidence():
    scorecard = KnowledgeScorecard(
        answers=[
            KnowledgeAnswerVerdict(
                question_id="1", verdict="correct", explanation="ok",
                correct_answer="A", student_answer="A", confidence="high",
            )
        ]
    )
    response, review_required = ks._build_knowledge_response(
        scorecard,
        exam={"test_id": 5, "test_name": "HW"},
        subject="Science",
        is_handwritten=True,
        answers_pdf_url="https://x/a.pdf",
        page_count=3,
        confidences={"1": 0.2},  # below the default 0.75 threshold
    )
    assert review_required is True
    q1 = response.questions[0]
    assert q1.low_confidence is True
    assert q1.ocr_confidence == 0.2
    assert response.page_count == 3
    assert any("OCR confidence" in f for f in response.review_flags)


# --- grade_knowledge_submission guards ---------------------------------------

async def test_grade_knowledge_submission_requires_questions_pdf():
    exam = {"test_id": 5, "questions_pdf_url": None, "test_name": "HW"}
    job = {"is_handwritten": 0, "answers_json": "{}"}
    with pytest.raises(ValueError, match="questions_pdf_url"):
        await ks.grade_knowledge_submission(MagicMock(), exam=exam, job=job, subject="S")


# --- _do_grade routes rubric-free jobs to the knowledge path -----------------

async def test_do_grade_routes_rubric_free_and_skips_rubric_path():
    job = {"exam_id": 5, "is_handwritten": 0, "student_id": 7}
    exam = {
        "id": 5,
        "test_id": 212,
        "course_id": "16",
        "test_name": "Chapter 6 Homework",
        "assessment_type": "homework",
        "marking_scheme_pdf_url": None,
        "is_handwritten": 0,
    }
    db = MagicMock()
    db.query_one = AsyncMock(side_effect=[job, exam])
    db.write = AsyncMock(return_value=1)

    response = GradedScorecardResponse(
        test_id=212,
        subject="AP Biology",
        generated_at="2026-08-05T00:00:00Z",
        grading_mode="knowledge",
        percentage=0.0,
        total_points_earned=0.0,
        total_points_possible=0.0,
        correct_count=1,
        questions_total=2,
        questions_graded=2,
        is_handwritten=False,
    )

    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(grader_job_service, "require_langfuse_active"),
        patch.object(grader_job_service, "set_trace_attributes"),
        patch.object(grader_job_service, "record_trace_output"),
        patch.object(
            grader_job_service,
            "get_course_config",
            new=AsyncMock(return_value={"course_name": "AP Biology", "exam_body": "College Board"}),
        ),
        patch.object(grader_job_service, "get_grading_addendum", new=AsyncMock(return_value="")),
        patch.object(grader_job_service, "get_ocr_addendum", new=AsyncMock(return_value="")),
        patch.object(grader_job_service, "get_gemini_client", return_value=MagicMock()),
        patch.object(grader_job_service, "is_rubric_free", return_value=True),
        patch.object(
            grader_job_service,
            "grade_knowledge_submission",
            new=AsyncMock(return_value=(response, True)),
        ) as mock_knowledge,
        patch.object(grader_job_service, "grade_submission") as mock_grade,
        patch.object(grader_job_service, "_attach_summaries", new=AsyncMock()) as mock_summaries,
    ):
        await grader_job_service._do_grade("jobk")

    mock_knowledge.assert_awaited_once()
    # The rubric grade path and summaries are bypassed entirely.
    mock_grade.assert_not_called()
    mock_summaries.assert_not_awaited()
    # Persisted as succeeded with review_required=1 (True) and the knowledge scorecard.
    sql, params = db.write.call_args.args
    assert "status='succeeded'" in sql
    assert params["r"] == 1
    assert json.loads(params["s"])["grading_mode"] == "knowledge"
