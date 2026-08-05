"""Unit tests for the inline typed-answer submission path.

Non-handwritten exams now submit answers inline as ``{major_qid: text}`` and are
graded with no OCR and no DB fetch. ``_build_typed_submission`` must read the
stored ``answers_json``, normalize the keys to the rubric's canonical form,
reconcile them with the rubric, and forward them to the vendored
``label_typed_answers``. The key-reconciliation step is stubbed to a passthrough
here (it has its own tests in ``test_grader_answer_map.py``).
"""
import json
from unittest.mock import MagicMock, patch

from app.services import grader_job_service
from app.services.grader_answer_map_service import RemapResult


def _passthrough_remap(client, *, answers_by_major, rubric):
    """Stand-in for ``remap_typed_answers``: forward the answers unchanged (fast path)."""
    return RemapResult(answers_by_major=answers_by_major)


async def test_inline_answers_normalized_and_forwarded():
    """A JSON-string ``answers_json`` is parsed, key-normalized, and forwarded."""
    job = {
        "answers_json": json.dumps({"1": "ans one", " 2 ": "ans two", "3A": "ans 3a"}),
        "student_id": 7,
    }
    fake_submission = MagicMock()
    fake_label = MagicMock(return_value=(fake_submission, ["3a"]))

    with (
        patch.object(grader_job_service, "label_typed_answers", fake_label),
        patch.object(grader_job_service, "remap_typed_answers", _passthrough_remap),
    ):
        submission, ai_labelled, _remap = await grader_job_service._build_typed_submission(
            MagicMock(), {}, job, MagicMock()
        )

    assert submission is fake_submission
    assert ai_labelled == ["3a"]
    _, kwargs = fake_label.call_args
    # "1" stays, " 2 " -> "2", "3A" -> "3a" (canonical lowercase + trimmed).
    assert kwargs["answers_by_major"] == {"1": "ans one", "2": "ans two", "3a": "ans 3a"}


async def test_inline_answers_accept_dict_payload():
    """If the driver returns the JSON column already decoded to a dict, it works too."""
    job = {"answers_json": {"1": "x", "2": "y"}, "student_id": 7}
    fake_label = MagicMock(return_value=(MagicMock(), []))

    with (
        patch.object(grader_job_service, "label_typed_answers", fake_label),
        patch.object(grader_job_service, "remap_typed_answers", _passthrough_remap),
    ):
        await grader_job_service._build_typed_submission(MagicMock(), {}, job, MagicMock())

    _, kwargs = fake_label.call_args
    assert kwargs["answers_by_major"] == {"1": "x", "2": "y"}
