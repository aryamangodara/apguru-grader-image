"""Pydantic schemas for the AP FRQ Auto-Grader.

These types serve double duty: application data classes and Gemini
`response_schema` definitions for structured output. Field descriptions
are surfaced to Gemini and materially affect output quality, so keep
them precise.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------

class RubricPoint(BaseModel):
    point_id: str = Field(
        description="Stable identifier for this rubric point, e.g. '1a-i' or 'Q2.B.1'."
    )
    question_id: str = Field(
        description="Which question/sub-part this point belongs to, e.g. '1a' or '2'."
    )
    point_value: float = Field(
        description="Points awarded for satisfying this criterion (usually 1.0)."
    )
    criterion: str = Field(
        description="Exact statement of what the student must demonstrate to earn this point."
    )
    examples_credit: list[str] = Field(
        default_factory=list,
        description="Phrases or answers that earn the point.",
    )
    examples_no_credit: list[str] = Field(
        default_factory=list,
        description="Phrases or answers that do not earn the point.",
    )
    notes: str | None = Field(
        default=None,
        description="Additional scorer notes from the rubric, if present.",
    )


class QuestionRubric(BaseModel):
    question_id: str = Field(description="Top-level question identifier, e.g. '1', '2', 'FRQ-3'.")
    prompt_summary: str = Field(description="The question stem, trimmed to its essence.")
    rubric_points: list[RubricPoint]
    max_points: float


class ParsedRubric(BaseModel):
    subject: str
    year: int
    set_label: str | None = None
    source_url: str = Field(
        default="local marking scheme",
        description="The AP Central URL the rubric was downloaded from, or 'local marking scheme' if user-supplied.",
    )
    total_points: float
    questions: list[QuestionRubric]
    parse_warnings: list[str] = Field(
        default_factory=list,
        description="Anything ambiguous during extraction (e.g. 'Q3 sub-parts ambiguous').",
    )


# ---------------------------------------------------------------------------
# OCR / submission
# ---------------------------------------------------------------------------

class TranscribedAnswer(BaseModel):
    question_id: str = Field(
        description="Question/sub-part identifier, e.g. '1a' or 'FRQ-2'."
    )
    transcript: str = Field(
        description=(
            "Verbatim plaintext transcription of the student's handwriting. "
            "Math expressions in LaTeX between $...$ (inline) or $$...$$ (display). "
            "Code (Java etc.) in plain text with original indentation."
        )
    )
    confidence: float = Field(
        description="Self-reported confidence in transcription accuracy, in [0, 1]."
    )
    source_pages: list[int] = Field(
        description="Page numbers (1-indexed) where this answer appears."
    )
    low_confidence_snippets: list[str] = Field(
        default_factory=list,
        description="Specific snippets the model is least sure about.",
    )


class ParsedSubmission(BaseModel):
    answers: list[TranscribedAnswer]
    unassigned_text: list[str] = Field(
        default_factory=list,
        description="Text that could not be confidently mapped to any question.",
    )
    page_count: int


# ---------------------------------------------------------------------------
# Scorecard
# ---------------------------------------------------------------------------

class RubricPointScore(BaseModel):
    point_id: str
    awarded: bool
    points_earned: float
    rationale: str = Field(
        description="Explanation citing the transcript for why this point was awarded or denied."
    )
    transcript_evidence: str = Field(
        description="The exact snippet from the transcript that supports the decision."
    )
    grading_confidence: Literal["high", "medium", "low"]
    review_recommended: bool = Field(
        default=False,
        description="True if upstream OCR confidence was below threshold for this question.",
    )


class QuestionScorecard(BaseModel):
    question_id: str
    points_earned: float
    points_possible: float
    point_scores: list[RubricPointScore]
    transcript_used: str = Field(
        description="Echo of the transcript that was graded, for audit."
    )
    summary_comment: str = Field(
        default="",
        description=(
            "A 1-2 sentence overall assessment of the student's answer to this "
            "question, addressed to the student: what earned credit and what cost "
            "marks. Summarize — do not restate every rubric point."
        ),
    )


class Scorecard(BaseModel):
    """Final assembled scorecard. Not a Gemini response_schema — assembled in code."""
    subject: str
    year: int
    set_label: str | None = None
    total_points_earned: float
    total_points_possible: float
    percentage: float = Field(ge=0.0, le=100.0)
    questions: list[QuestionScorecard]
    review_flags: list[str] = Field(default_factory=list)
    generated_at: str
    config_echo: dict[str, Any] = Field(default_factory=dict)
