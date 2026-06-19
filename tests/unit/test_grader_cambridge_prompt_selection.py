"""Unit tests for exam-body-aware grader prompt selection — Cambridge IGCSE/A-Level.

Cambridge IGCSE / A-Level courses are seeded with
``exam_body = 'Cambridge IGCSE/A-Level'`` (central migration 031). The grader must then parse
rubrics and grade with the Cambridge prompt variants instead of the AP or IB ones —
without any API/schema change. These tests cover both the pure selector functions and
the ``register_exam`` wiring that feeds the selected ``prompt_path`` into the vendored
``parse_rubric_pdf``. Mirrors ``test_grader_ib_prompt_selection.py``.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.grader_schema import RegisterExamRequest
from app.services import grader_exam_service
from app.services.grader import GRADE_PROMPT, RUBRIC_PROMPT
from app.services.grader_prompts import (
    GRADE_PROMPT_CAMBRIDGE,
    RUBRIC_PROMPT_CAMBRIDGE,
    grade_prompt_for,
    is_cambridge_exam_body,
    rubric_prompt_for,
)

CAMBRIDGE = "Cambridge IGCSE/A-Level"

# --- pure selectors ----------------------------------------------------------

def test_cambridge_exam_body_detection_is_case_and_space_insensitive():
    assert is_cambridge_exam_body(CAMBRIDGE)
    assert is_cambridge_exam_body("cambridge igcse/a-level")
    assert is_cambridge_exam_body("  Cambridge IGCSE/A-Level  ")
    assert not is_cambridge_exam_body("IBO")
    assert not is_cambridge_exam_body("College Board")
    assert not is_cambridge_exam_body(None)
    assert not is_cambridge_exam_body("")


def test_rubric_prompt_selection_cambridge():
    assert rubric_prompt_for(CAMBRIDGE) == RUBRIC_PROMPT_CAMBRIDGE
    assert rubric_prompt_for("cambridge igcse/a-level") == RUBRIC_PROMPT_CAMBRIDGE
    # Other bodies are unaffected by the new branch.
    assert rubric_prompt_for("College Board") == RUBRIC_PROMPT
    assert rubric_prompt_for(None) == RUBRIC_PROMPT


def test_grade_prompt_selection_cambridge():
    assert grade_prompt_for(CAMBRIDGE) == GRADE_PROMPT_CAMBRIDGE
    assert grade_prompt_for("cambridge igcse/a-level") == GRADE_PROMPT_CAMBRIDGE
    assert grade_prompt_for("College Board") == GRADE_PROMPT
    assert grade_prompt_for(None) == GRADE_PROMPT


def test_cambridge_prompt_files_exist_and_are_non_empty():
    # The selectors must point at real, non-empty prompt files.
    for path in (RUBRIC_PROMPT_CAMBRIDGE, GRADE_PROMPT_CAMBRIDGE):
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
        test_id=8071,
        course_id="71",
        test_name="IGCSE English (smoke)",
        is_handwritten=False,  # typed → no questions_pdf required by the validator
        marking_scheme_pdf_url="https://example.com/ms.pdf",
    )
    db = MagicMock()
    db.write = AsyncMock()
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
                return_value={"course_name": "IGCSE English", "exam_body": exam_body}
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


async def test_register_cambridge_course_parses_with_cambridge_rubric_prompt():
    assert await _prompt_path_for_register("Cambridge IGCSE/A-Level") == RUBRIC_PROMPT_CAMBRIDGE


async def test_register_non_cambridge_course_parses_with_default_rubric_prompt():
    assert await _prompt_path_for_register("College Board") == RUBRIC_PROMPT
