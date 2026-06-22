"""Unit tests for exam-body-aware grader prompt selection (AP vs IB).

IB courses are seeded with ``exam_body = 'IBO'`` (migration 028). The grader must
then parse rubrics and grade with the IB prompt variants instead of the AP ones —
without any API/schema change. These tests cover both the pure selector functions
and the ``register_exam`` wiring that feeds the selected ``prompt_path`` into the
vendored ``parse_rubric_pdf``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.grader_schema import RegisterExamRequest
from app.services import grader_exam_service
from app.services.grader import GRADE_PROMPT, RUBRIC_PROMPT
from app.services.grader_prompts import (
    GRADE_PROMPT_IB,
    RUBRIC_PROMPT_IB,
    grade_prompt_for,
    is_ib_exam_body,
    rubric_prompt_for,
)

# --- pure selectors ----------------------------------------------------------

def test_ib_exam_body_detection_is_case_and_space_insensitive():
    assert is_ib_exam_body("IBO")
    assert is_ib_exam_body("ibo")
    assert is_ib_exam_body("  IBO  ")
    assert not is_ib_exam_body("College Board")
    assert not is_ib_exam_body(None)
    assert not is_ib_exam_body("")


def test_rubric_prompt_selection():
    assert rubric_prompt_for("IBO") == RUBRIC_PROMPT_IB
    assert rubric_prompt_for("ibo") == RUBRIC_PROMPT_IB
    assert rubric_prompt_for("College Board") == RUBRIC_PROMPT
    assert rubric_prompt_for(None) == RUBRIC_PROMPT


def test_grade_prompt_selection():
    assert grade_prompt_for("IBO") == GRADE_PROMPT_IB
    assert grade_prompt_for("ibo") == GRADE_PROMPT_IB
    assert grade_prompt_for("College Board") == GRADE_PROMPT
    assert grade_prompt_for(None) == GRADE_PROMPT


def test_ib_prompt_files_exist_and_are_non_empty():
    # The selectors must point at real, non-empty prompt files.
    for path in (RUBRIC_PROMPT_IB, GRADE_PROMPT_IB):
        assert path.is_file()
        assert path.read_text(encoding="utf-8").strip()


# --- register_exam wiring (exam_body → prompt_path) --------------------------

def _fake_rubric() -> MagicMock:
    rubric = MagicMock()
    rubric.total_points = 12.0
    rubric.questions = [MagicMock()]
    rubric.parse_warnings = []
    rubric.model_dump_json.return_value = "{}"
    return rubric


async def _prompt_path_for_register(exam_body: str):
    """Run register_exam with a cache miss and capture the prompt_path it parses with."""
    req = RegisterExamRequest(
        test_id=8116,
        course_id="116",
        test_name="IB BM HL (smoke)",
        is_handwritten=False,  # typed → no questions_pdf required by the validator
        marking_scheme_pdf_url="https://example.com/ms.pdf",
    )
    db = MagicMock()
    db.write = AsyncMock()
    # issue #11: register_exam now validates test_id against `tests` via query_one.
    db.query_one = AsyncMock(return_value={"id": 1})
    captured: dict = {}

    def fake_parse(*_args, **kwargs):
        captured["prompt_path"] = kwargs.get("prompt_path")
        return _fake_rubric()

    with (
        patch.object(grader_exam_service.Database, "get_instance", return_value=db),
        patch.object(grader_exam_service, "get_exam", new=AsyncMock(return_value=None)),
        patch.object(
            grader_exam_service,
            "get_course_config",
            new=AsyncMock(
                return_value={"course_name": "IB Business Management HL", "exam_body": exam_body}
            ),
        ),
        patch.object(grader_exam_service, "get_gemini_client", return_value=MagicMock()),
        patch.object(
            grader_exam_service, "fetch_pdf_to_tempfile", new=AsyncMock(return_value=MagicMock())
        ),
        patch.object(grader_exam_service, "parse_rubric_pdf", new=MagicMock(side_effect=fake_parse)),
    ):
        await grader_exam_service.register_exam(req)
    return captured["prompt_path"]


async def test_register_ibo_course_parses_with_ib_rubric_prompt():
    assert await _prompt_path_for_register("IBO") == RUBRIC_PROMPT_IB


async def test_register_non_ib_course_parses_with_default_rubric_prompt():
    assert await _prompt_path_for_register("College Board") == RUBRIC_PROMPT
