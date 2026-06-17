"""Request/response schemas for the AP FRQ auto-grader API.

`GradedScorecardResponse` is the UI-complete contract the frontend renders
directly — it composes the vendored grader's `Scorecard` with rubric criteria
and submission confidence (assembled in `grader/response_builder.py`).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_serializer, model_validator

JobStatus = Literal["queued", "running", "succeeded", "failed"]


# --- exam registration ------------------------------------------------------

class RegisterExamRequest(BaseModel):
    test_id: int = Field(
        description="tests.id this exam grades — the identifier used by every grader API."
    )
    course_id: str = Field(
        description="course_configs.course_id this exam belongs to — drives the "
        "subject name and grading/OCR addenda resolved at grade time."
    )
    test_name: str = Field(
        min_length=1,
        description="Human-readable exam label (e.g. 'March 2024 Set 1') shown on the scorecard.",
    )
    is_handwritten: bool = Field(
        description="True = handwritten (answers from a PDF, OCR'd); False = typed "
        "(answers supplied inline at submission time)."
    )
    marking_scheme_pdf_url: str = Field(description="Durable URL to the marking-scheme PDF.")
    questions_pdf_url: str | None = Field(
        default=None,
        description="Durable URL to the questions PDF — required for handwritten "
        "exams (used as OCR context); ignored for typed exams.",
    )

    @model_validator(mode="after")
    def _require_questions_pdf_for_handwritten(self) -> RegisterExamRequest:
        if self.is_handwritten and not self.questions_pdf_url:
            raise ValueError("questions_pdf_url is required for handwritten exams")
        return self


class RegisterExamResponse(BaseModel):
    test_id: int
    course_id: str
    subject: str
    test_name: str
    is_handwritten: bool
    total_points: float
    question_count: int
    parse_warnings: list[str] = Field(default_factory=list)
    cached: bool = Field(
        description="True if an already-parsed rubric was reused (no Gemini call)."
    )


# --- submission -------------------------------------------------------------

class CreateSubmissionRequest(BaseModel):
    student_id: int = Field(description="Student being graded (required).")
    answers_pdf_url: str | None = Field(
        default=None,
        description="Handwritten exams: durable URL to the student's answer PDF (OCR'd).",
    )
    answers: dict[str, str] | None = Field(
        default=None,
        description="Typed exams: answers supplied inline as {major_question_id: answer_text}, "
        'e.g. {"1": "...", "2": "..."}. Graded directly — no OCR.',
    )


class CreateSubmissionResponse(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"


# --- graded scorecard (UI-complete contract) --------------------------------

class GradedPoint(BaseModel):
    point_id: str
    criterion: str | None = Field(default=None, description="What this point tests (from the rubric).")
    awarded: bool
    points_earned: float
    points_possible: float
    rationale: str = Field(description="Why the point was awarded or denied.")
    transcript_evidence: str = Field(description="Exact quote backing the decision.")
    grading_confidence: Literal["high", "medium", "low"]
    review_recommended: bool = False


class GradedQuestion(BaseModel):
    question_id: str
    prompt_summary: str | None = Field(default=None, description="Question stem essence (from the rubric).")
    comment: str = Field(default="", description="Per-question overall summary, addressed to the student.")
    points_earned: float
    points_possible: float
    status: Literal["graded", "unattempted", "recovered", "merged"]
    transcript: str = Field(default="", description="The transcript that was graded.")
    ocr_confidence: float | None = Field(default=None, description="None for typed answers.")
    low_confidence: bool = False
    source_pages: list[int] = Field(default_factory=list, description="Answer pages this maps to (handwritten).")
    tags: list[str] = Field(default_factory=list)
    points: list[GradedPoint] = Field(default_factory=list)


class QuestionMarks(BaseModel):
    """Earned marks for one major question — a flat map for the frontend to render."""

    question_id: str = Field(description="Major question number, e.g. '1', '2'.")
    marks: float = Field(description="Total marks earned for the question (sub-parts summed).")

    @field_serializer("marks")
    def _serialize_marks(self, value: float) -> float | int:
        """Whole marks render as an int (6); fractional stay a float (0.5)."""
        return int(value) if value == int(value) else value


class GradedScorecardResponse(BaseModel):
    test_id: int
    subject: str
    test_name: str | None = None
    generated_at: str
    percentage: float
    total_points_earned: float
    total_points_possible: float
    question_wise_marks: list[QuestionMarks] = Field(
        default_factory=list,
        description="Earned marks per major question (sub-parts summed), for direct "
        "mapping to questions.",
    )
    questions_graded: int
    review_flags: list[str] = Field(default_factory=list)
    is_handwritten: bool
    answers_pdf_url: str | None = Field(
        default=None, description="So the frontend can render the scanned pages itself."
    )
    page_count: int | None = None
    questions: list[GradedQuestion] = Field(default_factory=list)
    unattempted: list[GradedQuestion] = Field(default_factory=list)


# --- job polling ------------------------------------------------------------

class GradingJobResponse(BaseModel):
    job_id: str
    test_id: int
    student_id: int
    status: JobStatus
    is_handwritten: bool
    review_required: bool = False
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    scorecard: GradedScorecardResponse | None = Field(
        default=None, description="Present once status == 'succeeded'."
    )
    error: str | None = Field(default=None, description="Present once status == 'failed'.")


# --- exam listing -----------------------------------------------------------

class ExamSummary(BaseModel):
    test_id: int
    course_id: str
    subject: str
    test_name: str
    is_handwritten: bool
    total_points: float | None = None
    parse_warnings: list[str] = Field(default_factory=list)
    questions_pdf_url: str | None = None
    marking_scheme_pdf_url: str | None = None
    rubric_parsed_at: str | None = None
    created_at: str | None = None


class ExamListResponse(BaseModel):
    count: int
    exams: list[ExamSummary] = Field(default_factory=list)
