"""Unit tests for typed answer -> rubric-question content mapping.

When a typed submission's answer keys don't match the rubric's question ids, the grader
content-maps each answer onto the question(s) it answers, or fails the job loudly. These
cover the ``map_answers_to_questions`` primitive, the ``remap_typed_answers`` orchestration
(fast-path plus every fail-loud trigger), the ``_do_grade`` wiring that flags remapped
questions for review and records the mapping, and the ``run_grading_job`` failure that
persists the ``ANSWER_MAPPING_FAILED`` code. DB + Gemini are mocked — no real SQL/LLM runs.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors import AnswerMappingError
from app.schemas.grader_schema import AppliedAnswerMap, GradedScorecardResponse
from app.services import grader_answer_map_service as ams
from app.services import grader_job_service
from app.services.grader import answer_map
from app.services.grader.schemas import (
    AnswerQuestionMap,
    MappedAnswer,
    ParsedRubric,
    QuestionRubric,
    RubricPoint,
)
from app.services.grader_answer_map_service import RemapResult


def _rubric(qids: list[str]) -> ParsedRubric:
    return ParsedRubric(
        subject="World History",
        year=0,
        total_points=float(len(qids)),
        questions=[
            QuestionRubric(
                question_id=q,
                prompt_summary=f"stem for {q}",
                rubric_points=[
                    RubricPoint(point_id=f"{q}-1", question_id=q, point_value=1.0, criterion=f"crit {q}")
                ],
                max_points=1.0,
            )
            for q in qids
        ],
    )


def _map(by_key: dict[str, tuple[list[str], float]]) -> AnswerQuestionMap:
    """Build an AnswerQuestionMap from ``{submitted_key: (question_ids, confidence)}``."""
    return AnswerQuestionMap(
        mappings=[
            MappedAnswer(submitted_key=k, question_ids=qids, confidence=conf)
            for k, (qids, conf) in by_key.items()
        ]
    )


def _run_remap(answers: dict[str, str], rubric: ParsedRubric, mapping: AnswerQuestionMap) -> RemapResult:
    with (
        patch.object(ams, "gemini_generation_reporter", return_value=None),
        patch.object(ams, "map_answers_to_questions", return_value=mapping),
    ):
        return ams.remap_typed_answers(MagicMock(), answers_by_major=answers, rubric=rubric)


# --- map_answers_to_questions primitive --------------------------------------

def test_map_answers_primitive_returns_parsed_and_includes_context():
    resp = MagicMock()
    resp.parsed = _map({"21": (["216a"], 0.95)})
    with patch.object(answer_map, "generate_with_retry", return_value=resp) as gen:
        out = answer_map.map_answers_to_questions(
            MagicMock(), answers={"21": "essay about voyages"}, rubric=_rubric(["216a", "217a"])
        )
    assert isinstance(out, AnswerQuestionMap)
    assert out.mappings[0].question_ids == ["216a"]
    # The rubric stems and the answer text/key are all handed to the model.
    blob = "\n".join(str(c) for c in gen.call_args.kwargs["contents"])
    assert "216a" in blob
    assert "stem for 216a" in blob
    assert "answer key: 21" in blob
    assert "essay about voyages" in blob


# --- remap_typed_answers: fast path (no LLM call) ----------------------------

def test_remap_fast_path_keys_already_match():
    rubric = _rubric(["216a", "216b", "218"])
    answers = {"216a": "a", "218": "d"}  # a subset of the rubric ids → trusted as-is
    with patch.object(ams, "map_answers_to_questions") as mapper:
        result = ams.remap_typed_answers(MagicMock(), answers_by_major=answers, rubric=rubric)
    mapper.assert_not_called()
    assert result.remapped is False
    assert result.answers_by_major == answers
    assert result.remapped_qids == []
    assert result.applied_map == []


def test_remap_fast_path_feature_disabled():
    rubric = _rubric(["216a"])
    answers = {"21": "a"}  # mismatch, but the feature flag is off → still no call
    with (
        patch.object(ams.settings, "grader_enable_answer_map", False),
        patch.object(ams, "map_answers_to_questions") as mapper,
    ):
        result = ams.remap_typed_answers(MagicMock(), answers_by_major=answers, rubric=rubric)
    mapper.assert_not_called()
    assert result.remapped is False
    assert result.answers_by_major == answers


def test_remap_fast_path_empty_answers():
    with patch.object(ams, "map_answers_to_questions") as mapper:
        result = ams.remap_typed_answers(MagicMock(), answers_by_major={}, rubric=_rubric(["1"]))
    mapper.assert_not_called()
    assert result.remapped is False


# --- remap_typed_answers: successful content map -----------------------------

def test_remap_success_rekeys_and_flags():
    # quiz-861 shape: one SAQ blob answers three sub-parts; keys don't match rubric ids.
    rubric = _rubric(["216a", "216b", "216c", "218", "220"])
    answers = {"21": "voyages...", "23": "argument...", "24": "documents..."}
    mapping = _map(
        {
            "21": (["216a", "216b", "216c"], 0.95),
            "23": (["218"], 0.9),
            "24": (["220"], 0.92),
        }
    )
    result = _run_remap(answers, rubric, mapping)
    assert result.remapped is True
    # Rekeyed onto rubric ids; the SAQ blob is copied across each of its sub-parts.
    assert result.answers_by_major == {
        "216a": "voyages...",
        "216b": "voyages...",
        "216c": "voyages...",
        "218": "argument...",
        "220": "documents...",
    }
    assert result.remapped_qids == ["216a", "216b", "216c", "218", "220"]
    assert {a.submitted_key: a.mapped_question_ids for a in result.applied_map} == {
        "21": ["216a", "216b", "216c"],
        "23": ["218"],
        "24": ["220"],
    }


def test_remap_drops_invented_ids_but_keeps_valid():
    rubric = _rubric(["216a", "218"])
    mapping = _map({"21": (["216a", "999"], 0.9)})  # 999 isn't in the rubric → dropped
    result = _run_remap({"21": "x"}, rubric, mapping)
    assert result.remapped is True
    assert result.answers_by_major == {"216a": "x"}
    assert result.applied_map[0].mapped_question_ids == ["216a"]


# --- remap_typed_answers: fail-loud triggers ---------------------------------

def test_remap_orphan_answer_fails_loud():
    mapping = _map({"21": ([], 0.9)})  # matched nothing
    with pytest.raises(AnswerMappingError, match="matched no rubric question"):
        _run_remap({"21": "x"}, _rubric(["216a"]), mapping)


def test_remap_low_confidence_fails_loud():
    mapping = _map({"21": (["216a"], 0.4)})  # below the 0.7 default threshold
    with pytest.raises(AnswerMappingError, match=r"confidence 0\.40"):
        _run_remap({"21": "x"}, _rubric(["216a"]), mapping)


def test_remap_collision_fails_loud():
    rubric = _rubric(["216a", "216b"])
    mapping = _map({"21": (["216a"], 0.9), "22": (["216a"], 0.9)})  # both claim 216a
    with pytest.raises(AnswerMappingError, match="claimed by both"):
        _run_remap({"21": "x", "22": "y"}, rubric, mapping)


def test_remap_dropped_answer_fails_loud():
    rubric = _rubric(["216a", "218"])
    mapping = _map({"21": (["216a"], 0.9)})  # the model forgot to map "23"
    with pytest.raises(AnswerMappingError, match="was not mapped"):
        _run_remap({"21": "x", "23": "y"}, rubric, mapping)


# --- run_grading_job: fail-loud persists the machine-readable code ------------

async def test_run_grading_job_persists_mapping_error_code():
    db = MagicMock()
    db.write = AsyncMock()
    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(
            grader_job_service,
            "_do_grade",
            new=AsyncMock(side_effect=AnswerMappingError("no answers matched the rubric")),
        ),
    ):
        await grader_job_service.run_grading_job("jobk")
    # Last write is the failure; error_message leads with the stable code (machine-readable).
    sql, params = db.write.call_args.args
    assert "status='failed'" in sql
    assert params["e"].startswith("ANSWER_MAPPING_FAILED: ")
    assert "no answers matched the rubric" in params["e"]


# --- _do_grade: typed remap flags for review and records the mapping ----------

async def test_do_grade_typed_remap_flags_review_and_records_mapping():
    job = {
        "exam_id": 5, "is_handwritten": 0, "student_id": 7,
        "answers_json": "{}", "answers_pdf_url": None,
    }
    exam = {
        "id": 5, "test_id": 861, "course_id": "30", "test_name": "Quiz #861",
        "assessment_type": "quiz", "marking_scheme_pdf_url": "https://x/ms.pdf", "is_handwritten": 0,
    }
    db = MagicMock()
    db.query_one = AsyncMock(side_effect=[job, exam])
    db.write = AsyncMock(return_value=1)

    applied = [AppliedAnswerMap(submitted_key="21", mapped_question_ids=["216a", "216b"], confidence=0.95)]
    remap = RemapResult(
        answers_by_major={"216a": "v", "216b": "v"},
        remapped_qids=["216a", "216b"],
        applied_map=applied,
        remapped=True,
    )

    scorecard = MagicMock()
    scorecard.review_flags = ["216a flagged"]
    grade_result = {
        "scorecard": scorecard, "submission": MagicMock(),
        "recovered_qids": [], "merged_parent_answers": {}, "missing_qids": [],
    }
    built = GradedScorecardResponse(
        test_id=861, subject="World History", generated_at="2026-08-05T00:00:00Z",
        grading_mode="rubric", percentage=50.0, total_points_earned=8.0,
        total_points_possible=16.0, questions_graded=2, is_handwritten=False,
    )

    with (
        patch.object(grader_job_service.Database, "get_instance", return_value=db),
        patch.object(grader_job_service, "require_langfuse_active"),
        patch.object(grader_job_service, "set_trace_attributes"),
        patch.object(grader_job_service, "record_trace_output"),
        patch.object(
            grader_job_service,
            "get_course_config",
            new=AsyncMock(return_value={"course_name": "World History", "exam_body": "College Board"}),
        ),
        patch.object(grader_job_service, "get_grading_addendum", new=AsyncMock(return_value="")),
        patch.object(grader_job_service, "get_ocr_addendum", new=AsyncMock(return_value="")),
        patch.object(grader_job_service, "get_gemini_client", return_value=MagicMock()),
        patch.object(grader_job_service, "is_rubric_free", return_value=False),
        patch.object(grader_job_service, "get_cached_rubric", return_value=MagicMock()),
        patch.object(
            grader_job_service,
            "_build_typed_submission",
            new=AsyncMock(return_value=(MagicMock(), [], remap)),
        ),
        patch.object(grader_job_service, "grade_submission", return_value=grade_result) as mock_grade,
        patch.object(grader_job_service, "build_scorecard_response", return_value=built),
        patch.object(grader_job_service, "_record_job_output"),
        patch.object(grader_job_service, "_attach_summaries", new=AsyncMock()),
    ):
        await grader_job_service._do_grade("jobk")

    # The remapped questions were forced into human review.
    assert mock_grade.call_args.kwargs["force_review_qids"] == {"216a", "216b"}
    # The applied mapping is recorded on the persisted scorecard, and the job needs review.
    sql, params = db.write.call_args.args
    assert "status='succeeded'" in sql
    assert params["r"] == 1
    stored = json.loads(params["s"])
    assert stored["answer_mapping"] == [
        {"submitted_key": "21", "mapped_question_ids": ["216a", "216b"], "confidence": 0.95}
    ]
