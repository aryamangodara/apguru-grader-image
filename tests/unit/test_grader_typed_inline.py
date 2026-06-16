"""Unit tests for the inline typed-answer submission path.

Non-handwritten exams now submit answers inline as ``{major_qid: text}`` and are
graded with no OCR and no DB fetch. ``_build_typed_submission`` must read the
stored ``answers_json``, normalize the keys to the rubric's canonical form, and
forward them to the vendored ``label_typed_answers``.
"""
import json
from unittest.mock import MagicMock, patch

from app.services import grader_job_service


async def test_inline_answers_normalized_and_forwarded():
    """A JSON-string ``answers_json`` is parsed, key-normalized, and forwarded."""
    job = {
        "answers_json": json.dumps({"1": "ans one", " 2 ": "ans two", "3A": "ans 3a"}),
        "student_id": 7,
    }
    fake_submission = MagicMock()
    fake_label = MagicMock(return_value=(fake_submission, ["3a"]))

    with patch.object(grader_job_service, "label_typed_answers", fake_label):
        submission, ai_labelled = await grader_job_service._build_typed_submission(
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

    with patch.object(grader_job_service, "label_typed_answers", fake_label):
        await grader_job_service._build_typed_submission(MagicMock(), {}, job, MagicMock())

    _, kwargs = fake_label.call_args
    assert kwargs["answers_by_major"] == {"1": "x", "2": "y"}
