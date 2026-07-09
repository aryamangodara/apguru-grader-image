"""The grading thread pool must propagate the active trace context to workers.

``grade_questions_parallel`` fans each ``grade_question`` call out onto a
``ThreadPoolExecutor``. A bare pool does NOT copy contextvars the way
``asyncio.to_thread`` does, so before the fix every ``grader.grade`` generation
span created inside a worker orphaned onto its own Langfuse trace instead of
nesting under the job — making per-question grade cost invisible on the job
trace. This asserts the OTel context set before the call is visible inside the
worker threads (where the ``on_response`` tracing hook runs).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from opentelemetry import context as otel_context

from app.services.grader import GRADE_PROMPT
from app.services.grader.core import grade_questions_parallel
from app.services.grader.schemas import (
    QuestionRubric,
    QuestionScorecard,
    RubricPoint,
    RubricPointScore,
    TranscribedAnswer,
)


def _rubric(qid: str) -> QuestionRubric:
    return QuestionRubric(
        question_id=qid,
        prompt_summary="stem",
        rubric_points=[
            RubricPoint(point_id=f"{qid}-1", question_id=qid, point_value=1.0, criterion="c")
        ],
        max_points=1.0,
    )


def _answer(qid: str) -> TranscribedAnswer:
    return TranscribedAnswer(question_id=qid, transcript="ans", confidence=0.9, source_pages=[1])


def _scorecard(qid: str) -> QuestionScorecard:
    return QuestionScorecard(
        question_id=qid,
        points_earned=1.0,
        points_possible=1.0,
        point_scores=[
            RubricPointScore(
                point_id=f"{qid}-1", awarded=True, points_earned=1.0,
                rationale="r", transcript_evidence="e", grading_confidence="high",
            )
        ],
        transcript_used="ans",
        summary_comment="ok",
    )


def _fake_client() -> MagicMock:
    """A google-genai client whose generate_content returns a parsed scorecard."""
    resp = MagicMock()
    resp.parsed = _scorecard("x")
    resp.usage_metadata = None
    resp.text = "{}"
    client = MagicMock()
    client.models.generate_content.return_value = resp
    return client


def test_grade_workers_inherit_parent_otel_context():
    qids = ["1a", "1b", "1c"]
    rubric_by_qid = {q: _rubric(q) for q in qids}
    answer_by_qid = {q: _answer(q) for q in qids}

    # The on_response tracing hook runs *inside* the worker thread — capture what
    # trace context it observes there.
    key = otel_context.create_key("grader-test-parent")
    seen: list[object] = []

    def on_response(response, contents=None, label=""):
        seen.append(otel_context.get_value(key))

    # Simulate the active job trace by planting a marker in the current context,
    # exactly as the grader.job span would be present when grading runs.
    token = otel_context.attach(otel_context.set_value(key, "job-trace"))
    try:
        grade_questions_parallel(
            _fake_client(), qids, rubric_by_qid, answer_by_qid,
            subject="AP Test", prompt_path=GRADE_PROMPT,
            max_workers=3, on_response=on_response,
        )
    finally:
        otel_context.detach(token)

    # Every worker fired the hook AND saw the parent marker. Without the
    # context-propagation fix these would be [None, None, None].
    assert len(seen) == len(qids)
    assert all(v == "job-trace" for v in seen)
