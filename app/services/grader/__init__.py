"""Vendored AP FRQ auto-grader, packaged for the dashboard.

Synced from ``notebooks/Grader`` (``helpers.py`` -> ``core.py``, ``schemas.py``,
``prompts/``). The dashboard's grader feature imports the pipeline primitives and
Pydantic schemas from here; the orchestration/persistence lives in the
``app/services/grader_*.py`` services. Keep this package in sync with the source
repo — port logic changes back rather than diverging here.
"""
from pathlib import Path

from .core import (
    GradeSubmissionResult,
    flatten_rubric_by_subpart,
    get_gemini_client,
    grade_submission,
    label_typed_answers,
    ocr_submission,
    parse_rubric_pdf,
    render_pdf_to_images,
)
from .schemas import (
    ParsedRubric,
    ParsedSubmission,
    QuestionRubric,
    QuestionScorecard,
    RubricPoint,
    RubricPointScore,
    Scorecard,
    TranscribedAnswer,
)

# Prompt templates are read at call time; services pass these paths through.
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
OCR_PROMPT = PROMPTS_DIR / "ocr.txt"
RUBRIC_PROMPT = PROMPTS_DIR / "rubric_extract.txt"
GRADE_PROMPT = PROMPTS_DIR / "grade_question.txt"
SEGMENT_TYPED_PROMPT = PROMPTS_DIR / "segment_typed.txt"

__all__ = [
    # pipeline primitives
    "get_gemini_client",
    "render_pdf_to_images",
    "ocr_submission",
    "label_typed_answers",
    "parse_rubric_pdf",
    "flatten_rubric_by_subpart",
    "grade_submission",
    "GradeSubmissionResult",
    # schemas
    "ParsedRubric",
    "ParsedSubmission",
    "TranscribedAnswer",
    "QuestionRubric",
    "RubricPoint",
    "QuestionScorecard",
    "RubricPointScore",
    "Scorecard",
    # prompt paths
    "PROMPTS_DIR",
    "OCR_PROMPT",
    "RUBRIC_PROMPT",
    "GRADE_PROMPT",
    "SEGMENT_TYPED_PROMPT",
]
