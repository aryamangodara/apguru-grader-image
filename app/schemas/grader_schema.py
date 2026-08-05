"""Request/response schemas for the AP FRQ auto-grader API.

`GradedScorecardResponse` is the UI-complete contract the frontend renders
directly — it composes the vendored grader's `Scorecard` with rubric criteria
and submission confidence (assembled in `grader/response_builder.py`).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

JobStatus = Literal["queued", "running", "succeeded", "failed"]


# --- exam registration ------------------------------------------------------

class RegisterExamRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "test_id": 322,
                    "course_id": "14",
                    "test_name": "March 2024 Set 1",
                    "is_handwritten": True,
                    "marking_scheme_pdf_url": "https://files.example.com/ms/ap-bio-322.pdf",
                    "questions_pdf_url": "https://files.example.com/q/ap-bio-322.pdf",
                }
            ]
        }
    )

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
    marking_scheme_pdf_url: str | None = Field(
        default=None,
        description="Durable URL to the marking-scheme PDF. Required for exams and quizzes; "
        "optional for homework — omit it to grade from the model's own subject knowledge "
        "(right/wrong per question, no marks).",
    )
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
    test_id: int = Field(description="tests.id this exam grades.")
    course_id: str = Field(description="course_configs.course_id this exam belongs to.")
    subject: str = Field(description="Resolved subject name (e.g. 'AP Biology'), shown on the scorecard.")
    test_name: str = Field(description="Human-readable exam label shown on the scorecard.")
    is_handwritten: bool = Field(
        description="True for handwritten exams (PDF answers, OCR'd); False for typed."
    )
    total_points: float = Field(description="Total points available across the parsed rubric.")
    question_count: int = Field(description="Number of major questions in the parsed rubric.")
    parse_warnings: list[str] = Field(
        default_factory=list,
        description="Non-fatal notes from parsing the marking-scheme PDF (e.g. a skipped page).",
    )
    cached: bool = Field(
        description="True if an already-parsed rubric was reused (no Gemini call)."
    )


# --- submission -------------------------------------------------------------

class CreateSubmissionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "student_id": 1001,
                    "answers_pdf_url": "https://files.example.com/answers/1001.pdf",
                },
                {
                    "student_id": 1001,
                    "answers": {"1": "Mitochondria are the...", "2": "The independent variable is..."},
                },
            ]
        }
    )

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
    job_id: str = Field(description="Opaque job identifier; poll GET /grader/jobs/{job_id} for status.")
    status: Literal["queued"] = Field(
        default="queued",
        description="Always 'queued' on enqueue; grading runs in the background.",
    )


# --- graded scorecard (UI-complete contract) --------------------------------

class GradedPoint(BaseModel):
    point_id: str = Field(description="Rubric point identifier this score corresponds to.")
    criterion: str | None = Field(default=None, description="What this point tests (from the rubric).")
    awarded: bool = Field(description="True if this rubric point was awarded to the student.")
    points_earned: float = Field(description="Points earned for this rubric point.")
    points_possible: float = Field(description="Maximum points this rubric point is worth.")
    rationale: str = Field(description="Why the point was awarded or denied.")
    transcript_evidence: str = Field(description="Exact quote backing the decision.")
    grading_confidence: Literal["high", "medium", "low"] = Field(
        description="Model confidence in this point's grading decision."
    )
    review_recommended: bool = Field(
        default=False,
        description="True if a human should double-check this point (e.g. low confidence).",
    )


class GradedQuestion(BaseModel):
    question_id: str = Field(description="Question (or sub-part) identifier, e.g. '1', '3a-i'.")
    prompt_summary: str | None = Field(default=None, description="Question stem essence (from the rubric).")
    comment: str = Field(default="", description="Per-question overall summary, addressed to the student.")
    verdict: Literal["correct", "incorrect", "partial"] | None = Field(
        default=None,
        description="Knowledge-graded homework only (grading_mode='knowledge'): whether the "
        "answer is right. None for rubric-/marks-graded questions and for unattempted ones.",
    )
    correct_answer: str | None = Field(
        default=None,
        description="Knowledge-graded homework only: the correct/model answer to this question, "
        "from the model's own knowledge. None in rubric/marks mode.",
    )
    points_earned: float = Field(description="Total points earned for this question.")
    points_possible: float = Field(description="Maximum points this question is worth.")
    status: Literal["graded", "unattempted", "recovered", "merged"] = Field(
        description="How this question was scored: graded, unattempted (0/max), recovered "
        "(rescued from a parent-level answer), or merged (sub-parts combined)."
    )
    transcript: str = Field(default="", description="The transcript that was graded.")
    ocr_confidence: float | None = Field(default=None, description="None for typed answers.")
    low_confidence: bool = Field(
        default=False,
        description="True if the transcript/grading for this question is low-confidence.",
    )
    source_pages: list[int] = Field(default_factory=list, description="Answer pages this maps to (handwritten).")
    tags: list[str] = Field(default_factory=list, description="Topic/skill tags from the rubric, if any.")
    points: list[GradedPoint] = Field(
        default_factory=list, description="Per-rubric-point breakdown for this question."
    )


class QuestionMarks(BaseModel):
    """Earned marks for one major question — a flat map for the frontend to render."""

    question_id: str = Field(description="Major question number, e.g. '1', '2'.")
    marks: float = Field(description="Total marks earned for the question (sub-parts summed).")

    @field_serializer("marks")
    def _serialize_marks(self, value: float) -> float | int:
        """Whole marks render as an int (6); fractional stay a float (0.5)."""
        return int(value) if value == int(value) else value


class AppliedAnswerMap(BaseModel):
    """Records one submitted answer that had to be content-mapped onto rubric question(s).

    Present on the scorecard only when the submitted answer keys did NOT match the
    rubric's question ids and the grader matched answers to questions by content (see
    ``answer_mapping`` on :class:`GradedScorecardResponse`).
    """

    submitted_key: str = Field(description="The key the answer was submitted under (e.g. '21').")
    mapped_question_ids: list[str] = Field(
        description="Rubric question id(s) this answer was matched to and graded against."
    )
    confidence: float = Field(description="Model confidence in this match, in [0.0, 1.0].")


class GradedScorecardResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "test_id": 211,
                    "subject": "AP Biology",
                    "test_name": "Chapter 5 Homework",
                    "generated_at": "2026-08-05T10:30:00Z",
                    "grading_mode": "knowledge",
                    "percentage": 0,
                    "total_points_earned": 0,
                    "total_points_possible": 0,
                    "question_wise_marks": [],
                    "correct_count": 2,
                    "questions_total": 3,
                    "questions_graded": 3,
                    "is_handwritten": False,
                    "questions": [
                        {
                            "question_id": "1",
                            "comment": "Correct — you identified the mitochondrion as the site of aerobic respiration.",
                            "verdict": "correct",
                            "correct_answer": "The mitochondrion is the site of aerobic (cellular) respiration.",
                            "points_earned": 0,
                            "points_possible": 0,
                            "status": "graded",
                        },
                        {
                            "question_id": "2",
                            "comment": "Partially right: you named the independent but not the dependent variable.",
                            "verdict": "partial",
                            "correct_answer": "Independent variable: temperature; dependent variable: reaction rate.",
                            "points_earned": 0,
                            "points_possible": 0,
                            "status": "graded",
                        },
                    ],
                    "unattempted": [],
                }
            ]
        }
    )

    test_id: int = Field(description="tests.id of the graded exam.")
    subject: str = Field(description="Resolved subject name (e.g. 'AP Biology').")
    test_name: str | None = Field(default=None, description="Human-readable exam label, if known.")
    generated_at: str = Field(description="ISO-8601 timestamp when this scorecard was produced.")
    grading_mode: Literal["rubric", "knowledge"] = Field(
        default="rubric",
        description="How this was graded: 'rubric' = against a marking scheme, with marks "
        "(the default for exams/quizzes/homework with a marking scheme); 'knowledge' = homework "
        "graded from the model's own knowledge, right/wrong only (marks fields are 0 — read "
        "correct_count/questions_total and each question's verdict instead).",
    )
    percentage: float = Field(description="Final score as a percentage (0-100). Always 0 in 'knowledge' mode.")
    total_points_earned: float = Field(
        description="Sum of points earned across all questions. Always 0 in 'knowledge' mode."
    )
    total_points_possible: float = Field(
        description="Total points available across the exam. Always 0 in 'knowledge' mode."
    )
    question_wise_marks: list[QuestionMarks] = Field(
        default_factory=list,
        description="Earned marks per major question (sub-parts summed), for direct "
        "mapping to questions. Empty in 'knowledge' mode.",
    )
    correct_count: int | None = Field(
        default=None,
        description="'knowledge' mode only: how many questions the student got fully correct "
        "(the X in 'X of Y correct'). None in 'rubric' mode.",
    )
    questions_total: int | None = Field(
        default=None,
        description="'knowledge' mode only: total questions judged (the Y in 'X of Y correct'). "
        "None in 'rubric' mode.",
    )
    questions_graded: int = Field(description="Number of questions actually graded (excludes unattempted).")
    review_flags: list[str] = Field(
        default_factory=list,
        description="Scorecard-level flags a human should review (e.g. low-confidence questions).",
    )
    is_handwritten: bool = Field(description="True if graded from a handwritten PDF (OCR'd).")
    answers_pdf_url: str | None = Field(
        default=None, description="So the frontend can render the scanned pages itself."
    )
    page_count: int | None = Field(
        default=None, description="Number of answer pages rendered (handwritten only)."
    )
    questions: list[GradedQuestion] = Field(
        default_factory=list, description="Graded questions, in rubric order."
    )
    unattempted: list[GradedQuestion] = Field(
        default_factory=list, description="Questions with no answer found (scored 0/max)."
    )
    answer_mapping: list[AppliedAnswerMap] | None = Field(
        default=None,
        description="Present ONLY when the submitted typed answer keys did not match the rubric's "
        "question ids and the grader content-mapped each answer onto the question(s) it answers. "
        "Each entry records which rubric question(s) an answer was graded against, and the match "
        "confidence. When present, the affected questions are also flagged for human review "
        "(review_required=true) and the client should be fixed to key answers by rubric id. Null on "
        "the normal path where the keys already matched.",
    )
    # issue #14: post-grading audience summaries (empty when disabled or on failure).
    student_summary: str = Field(
        default="",
        description="Overall feedback addressed to the student (2-3 sentences): strengths, "
        "where marks were lost, and next steps.",
    )
    teacher_summary: str = Field(
        default="",
        description="Overall summary for the teacher (2-3 sentences): performance level, "
        "strengths, and gaps to reteach.",
    )
    parent_summary: str = Field(
        default="",
        description="Overall summary for the parent (2-3 sentences) in plain language: how "
        "the student did and how to support.",
    )


# --- job polling ------------------------------------------------------------

class GradingJobResponse(BaseModel):
    job_id: str = Field(description="Opaque job identifier (the one returned at submission).")
    test_id: int = Field(description="tests.id of the exam being graded.")
    student_id: int = Field(description="Student this job grades.")
    status: JobStatus = Field(description="Current lifecycle status of the grading job.")
    is_handwritten: bool = Field(description="True if grading a handwritten PDF (OCR'd).")
    review_required: bool = Field(
        default=False,
        description="True if the finished scorecard has items flagged for human review.",
    )
    created_at: str | None = Field(default=None, description="ISO-8601 time the job was enqueued.")
    started_at: str | None = Field(default=None, description="ISO-8601 time grading started, if begun.")
    finished_at: str | None = Field(default=None, description="ISO-8601 time grading finished, if done.")
    scorecard: GradedScorecardResponse | None = Field(
        default=None, description="Present once status == 'succeeded'."
    )
    error: str | None = Field(
        default=None,
        description="Present once status == 'failed'. A typed submission whose answers can't be "
        "content-mapped to the rubric fails here with 'ANSWER_MAPPING_FAILED: <detail>' (the stable "
        "code is prefixed so consumers can branch on it).",
    )


class JobSummary(BaseModel):
    """Lightweight grading-job row for the list view (no full scorecard)."""

    job_id: str = Field(description="Opaque job identifier; poll GET /grader/jobs/{job_id} for the scorecard.")
    test_id: int = Field(description="tests.id of the exam being graded.")
    student_id: int = Field(description="Student this job grades.")
    status: JobStatus = Field(description="Current lifecycle status of the grading job.")
    is_handwritten: bool = Field(description="True if grading a handwritten PDF (OCR'd).")
    review_required: bool = Field(
        default=False,
        description="True if the finished scorecard has items flagged for human review.",
    )
    percentage: float | None = Field(
        default=None, description="Final score %, present once status == 'succeeded'."
    )
    test_name: str | None = Field(default=None, description="Human-readable exam label, if known.")
    created_at: str | None = Field(default=None, description="ISO-8601 time the job was enqueued.")
    started_at: str | None = Field(default=None, description="ISO-8601 time grading started, if begun.")
    finished_at: str | None = Field(default=None, description="ISO-8601 time grading finished, if done.")
    error: str | None = Field(
        default=None,
        description="Present once status == 'failed'. A typed submission whose answers can't be "
        "content-mapped to the rubric fails here with 'ANSWER_MAPPING_FAILED: <detail>' (the stable "
        "code is prefixed so consumers can branch on it).",
    )


class JobListResponse(BaseModel):
    count: int = Field(description="Number of jobs returned.")
    jobs: list[JobSummary] = Field(default_factory=list, description="Matching jobs, newest first.")


# --- exam listing -----------------------------------------------------------

class ExamSummary(BaseModel):
    test_id: int = Field(description="tests.id this exam grades.")
    course_id: str = Field(description="course_configs.course_id this exam belongs to.")
    subject: str = Field(description="Resolved subject name (e.g. 'AP Biology').")
    test_name: str = Field(description="Human-readable exam label.")
    is_handwritten: bool = Field(description="True for handwritten exams (PDF answers, OCR'd).")
    total_points: float | None = Field(default=None, description="Total points in the parsed rubric, if parsed.")
    parse_warnings: list[str] = Field(
        default_factory=list, description="Non-fatal notes from parsing the marking-scheme PDF."
    )
    questions_pdf_url: str | None = Field(default=None, description="Durable URL to the questions PDF, if set.")
    marking_scheme_pdf_url: str | None = Field(
        default=None, description="Durable URL to the marking-scheme PDF, if set."
    )
    rubric_parsed_at: str | None = Field(
        default=None, description="ISO-8601 time the rubric was parsed & cached, if done."
    )
    created_at: str | None = Field(default=None, description="ISO-8601 time the exam was registered.")


class ExamListResponse(BaseModel):
    count: int = Field(description="Number of exams returned.")
    exams: list[ExamSummary] = Field(default_factory=list, description="Registered exams, newest first.")
