"""Unit tests for question_wise_marks aggregation in the grader scorecard.

The scorecard exposes a flat `question_wise_marks` array — earned marks per MAJOR
question — so a consumer can map marks to questions without walking the nested
`questions[].points[]` structure. Sub-parts ("1a","1b") roll up to their major ("1").
"""
from __future__ import annotations

from app.schemas.grader_schema import GradedQuestion, QuestionMarks
from app.services.grader.response_builder import _major_qid, build_question_wise_marks


def _gq(question_id: str, points_earned: float, status: str = "graded") -> GradedQuestion:
    return GradedQuestion(
        question_id=question_id,
        points_earned=points_earned,
        points_possible=max(points_earned, 0.0),
        status=status,
    )


def test_major_qid_extraction():
    assert _major_qid("1a") == "1"
    assert _major_qid("1b-i") == "1"
    assert _major_qid("3c-ii") == "3"
    assert _major_qid("5") == "5"
    assert _major_qid(" 2A ") == "2"        # normalized (trim+lower) then up to first digit run
    assert _major_qid("frq-3") == "frq-3"   # non-numeric prefix kept through the first digit run
    assert _major_qid("frq-3a") == "frq-3"  # non-numeric-prefixed sub-part rolls up to its major
    assert _major_qid("intro") == "intro"   # no digit at all -> fallback to the whole id


def test_subparts_roll_up_to_major():
    marks = build_question_wise_marks([_gq("1a", 3), _gq("1b", 3), _gq("2", 5)])
    assert [(m.question_id, m.marks) for m in marks] == [("1", 6.0), ("2", 5.0)]


def test_already_major_ids_pass_through():
    marks = build_question_wise_marks([_gq("1", 6), _gq("2", 2), _gq("3", 5)])
    assert [(m.question_id, m.marks) for m in marks] == [("1", 6.0), ("2", 2.0), ("3", 5.0)]


def test_unattempted_counted_as_zero():
    # Q1 fully unattempted; Q2 partially attempted (2a earned, 2b not).
    marks = build_question_wise_marks(
        [
            _gq("1a", 0, status="unattempted"),
            _gq("2a", 4),
            _gq("2b", 0, status="unattempted"),
        ]
    )
    assert [(m.question_id, m.marks) for m in marks] == [("1", 0.0), ("2", 4.0)]


def test_fractional_marks_sum():
    marks = build_question_wise_marks([_gq("1a", 0.5), _gq("1b", 1.0)])
    assert marks[0].question_id == "1"
    assert marks[0].marks == 1.5


def test_numeric_ordering_not_lexical():
    marks = build_question_wise_marks([_gq("10", 1), _gq("2", 1), _gq("1", 1)])
    assert [m.question_id for m in marks] == ["1", "2", "10"]

    # Non-numeric-prefixed ids sort naturally too (frq-2 before frq-10), after numerics.
    prefixed = build_question_wise_marks([_gq("frq-10", 1), _gq("frq-2", 1), _gq("frq-1", 1)])
    assert [m.question_id for m in prefixed] == ["frq-1", "frq-2", "frq-10"]


def test_marks_serialize_int_when_whole_float_when_fractional():
    assert QuestionMarks(question_id="1", marks=6.0).model_dump_json() == '{"question_id":"1","marks":6}'
    assert QuestionMarks(question_id="1", marks=0.5).model_dump_json() == '{"question_id":"1","marks":0.5}'


def test_marks_sum_equals_total_earned():
    questions = [_gq("1a", 3), _gq("1b", 3), _gq("2", 5), _gq("3a", 0, status="unattempted")]
    marks = build_question_wise_marks(questions)
    assert sum(m.marks for m in marks) == sum(q.points_earned for q in questions)
