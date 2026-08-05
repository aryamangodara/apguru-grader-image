"""Request/response schemas for the homework & quiz auto-grader endpoints.

The ``/grader/assessments`` surface reuses the exam grader's engine (rubric parse →
OCR/typed → grade → scorecard) but presents a self-contained contract keyed on
``(assessment_type, source_id)`` where ``source_id`` is the main-app source-table id
(``docs_homework_test.id`` for homework, ``quiz.id`` for quiz). The UI-complete
``GradedScorecardResponse`` and the submission request/response are reused verbatim
from :mod:`app.schemas.grader_schema`; only the registry/job envelopes differ (they
carry ``assessment_type`` + ``source_id`` instead of the exam surface's ``test_id``).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.grader_schema import GradedScorecardResponse, JobStatus

# The assessment types this surface grades. The exam/test surface is served by the
# separate /grader endpoints; 'exam' is intentionally NOT accepted here.
AssessmentType = Literal["homework", "quiz"]


# --- registration -----------------------------------------------------------

class RegisterAssessmentRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "summary": "Homework with a marking scheme (rubric-graded, with marks)",
                    "value": {
                        "assessment_type": "homework",
                        "source_id": 211,
                        "course_id": "16",
                        "title": "Chapter 5 Homework",
                        "is_handwritten": True,
                        "marking_scheme_pdf_url": "https://files.example.com/ms/hw-211.pdf",
                        "questions_pdf_url": "https://files.example.com/q/hw-211.pdf",
                    },
                },
                {
                    "summary": "Homework without a marking scheme (AI knowledge, right/wrong only)",
                    "value": {
                        "assessment_type": "homework",
                        "source_id": 212,
                        "course_id": "16",
                        "title": "Chapter 6 Homework",
                        "is_handwritten": False,
                        "questions_pdf_url": "https://files.example.com/q/hw-212.pdf",
                    },
                },
            ]
        }
    )

    assessment_type: AssessmentType = Field(
        description="What kind of assessment this is: 'homework' or 'quiz'. Together with "
        "source_id it uniquely identifies the assessment (a homework and a quiz can share a number)."
    )
    source_id: int = Field(
        description="Which specific assessment to grade — the source-table row id "
        "(docs_homework_test.id for homework, quiz.id for quiz), analogous to test_id for exams. "
        "Reused by submit/list to refer back to this registration."
    )
    course_id: str = Field(
        description="course_configs.course_id this assessment belongs to — drives the "
        "subject name and grading/OCR addenda resolved at grade time."
    )
    title: str = Field(
        min_length=1,
        description="Human-readable assessment label (e.g. 'Chapter 5 Homework') shown on the scorecard.",
    )
    is_handwritten: bool = Field(
        description="True = handwritten (answers from a PDF, OCR'd); False = typed "
        "(answers supplied inline at submission time)."
    )
    marking_scheme_pdf_url: str | None = Field(
        default=None,
        description="Durable URL to the marking-scheme PDF. Required for quizzes; optional for "
        "homework — omit it to grade from the model's own subject knowledge (right/wrong per "
        "question, no marks). When omitted, questions_pdf_url is required.",
    )
    questions_pdf_url: str | None = Field(
        default=None,
        description="Durable URL to the questions PDF. Required for handwritten assessments (OCR "
        "context) and for any homework graded without a marking scheme (the model reads the "
        "questions to judge answers from its own knowledge); optional for typed, marking-scheme-"
        "graded ones.",
    )

    @model_validator(mode="after")
    def _validate_pdfs(self) -> RegisterAssessmentRequest:
        # Quizzes always need a marking scheme; only homework may be graded without one.
        if self.assessment_type == "quiz" and not self.marking_scheme_pdf_url:
            raise ValueError("marking_scheme_pdf_url is required for quizzes")
        # Handwritten always needs the questions PDF (OCR context).
        if self.is_handwritten and not self.questions_pdf_url:
            raise ValueError("questions_pdf_url is required for handwritten assessments")
        # Graded without a marking scheme (homework knowledge mode) needs the questions PDF —
        # typed too — so the model can see each question and judge the answer itself.
        if not self.marking_scheme_pdf_url and not self.questions_pdf_url:
            raise ValueError(
                "questions_pdf_url is required when no marking_scheme_pdf_url is provided "
                "(the questions are needed to grade from the model's own knowledge)"
            )
        return self


class AssessmentRegistrationResponse(BaseModel):
    assessment_type: AssessmentType = Field(description="Whether this is a 'homework' or 'quiz'.")
    source_id: int = Field(description="Source-table id this assessment grades.")
    course_id: str = Field(description="course_configs.course_id this assessment belongs to.")
    subject: str = Field(description="Resolved subject name (e.g. 'AP Biology'), shown on the scorecard.")
    title: str = Field(description="Human-readable assessment label shown on the scorecard.")
    is_handwritten: bool = Field(
        description="True for handwritten assessments (PDF answers, OCR'd); False for typed."
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

class AssessmentSubmissionRequest(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "assessment_type": "homework",
                    "student_id": 1001,
                    "answers_pdf_url": "https://files.example.com/answers/1001.pdf",
                },
                {
                    "assessment_type": "homework",
                    "student_id": 1001,
                    "answers": {"1": "Mitochondria are the...", "2": "The independent variable is..."},
                },
            ]
        }
    )

    assessment_type: AssessmentType = Field(
        description="What kind of assessment this submission is for: 'homework' or 'quiz' "
        "(disambiguates source_id, which can repeat across types)."
    )
    student_id: int = Field(description="Student being graded (required).")
    answers_pdf_url: str | None = Field(
        default=None,
        description="Handwritten assessments: durable URL to the student's answer PDF (OCR'd).",
    )
    answers: dict[str, str] | None = Field(
        default=None,
        description="Typed assessments: answers supplied inline as {major_question_id: answer_text}, "
        "e.g. {\"1\": \"...\", \"2\": \"...\"}. Graded directly — no OCR.",
    )


# --- listing ----------------------------------------------------------------

class AssessmentSummary(BaseModel):
    assessment_type: AssessmentType = Field(description="Whether this is a 'homework' or 'quiz'.")
    source_id: int = Field(description="Source-table id this assessment grades.")
    course_id: str = Field(description="course_configs.course_id this assessment belongs to.")
    subject: str = Field(description="Resolved subject name (e.g. 'AP Biology').")
    title: str = Field(description="Human-readable assessment label.")
    is_handwritten: bool = Field(description="True for handwritten assessments (PDF answers, OCR'd).")
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
    created_at: str | None = Field(default=None, description="ISO-8601 time the assessment was registered.")


class AssessmentListResponse(BaseModel):
    count: int = Field(description="Number of assessments returned.")
    assessments: list[AssessmentSummary] = Field(
        default_factory=list, description="Registered assessments, newest first."
    )


# --- job polling ------------------------------------------------------------

class AssessmentJobResponse(BaseModel):
    job_id: str = Field(description="Opaque job identifier (the one returned at submission).")
    assessment_type: AssessmentType = Field(description="Whether this job grades a 'homework' or 'quiz'.")
    source_id: int = Field(description="Source-table id of the assessment being graded.")
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


class AssessmentJobSummary(BaseModel):
    """Lightweight grading-job row for the assessment list view (no full scorecard)."""

    job_id: str = Field(description="Opaque job identifier; poll GET /grader/assessments/jobs/{job_id}.")
    assessment_type: AssessmentType = Field(description="Whether this job grades a 'homework' or 'quiz'.")
    source_id: int = Field(description="Source-table id of the assessment being graded.")
    student_id: int = Field(description="Student this job grades.")
    status: JobStatus = Field(description="Current lifecycle status of the grading job.")
    is_handwritten: bool = Field(description="True if grading a handwritten PDF (OCR'd).")
    review_required: bool = Field(
        default=False,
        description="True if the finished scorecard has items flagged for human review.",
    )
    percentage: float | None = Field(
        default=None,
        description="Final score %, present once status == 'succeeded' (null for 'knowledge'-mode "
        "homework, which has no marks — read correct_count/questions_total instead).",
    )
    grading_mode: Literal["rubric", "knowledge"] | None = Field(
        default=None,
        description="How it was graded: 'rubric' (marks) or 'knowledge' (right/wrong), once succeeded.",
    )
    correct_count: int | None = Field(
        default=None,
        description="'knowledge' mode: questions correct (the X in 'X of Y'), once succeeded.",
    )
    questions_total: int | None = Field(
        default=None,
        description="'knowledge' mode: total questions judged (the Y in 'X of Y'), once succeeded.",
    )
    title: str | None = Field(default=None, description="Human-readable assessment label, if known.")
    created_at: str | None = Field(default=None, description="ISO-8601 time the job was enqueued.")
    started_at: str | None = Field(default=None, description="ISO-8601 time grading started, if begun.")
    finished_at: str | None = Field(default=None, description="ISO-8601 time grading finished, if done.")
    error: str | None = Field(
        default=None,
        description="Present once status == 'failed'. A typed submission whose answers can't be "
        "content-mapped to the rubric fails here with 'ANSWER_MAPPING_FAILED: <detail>' (the stable "
        "code is prefixed so consumers can branch on it).",
    )


class AssessmentJobListResponse(BaseModel):
    count: int = Field(description="Number of jobs returned.")
    jobs: list[AssessmentJobSummary] = Field(default_factory=list, description="Matching jobs, newest first.")
