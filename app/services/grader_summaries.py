"""Post-grading audience summaries (issue #14).

After a submission is graded, turn the assembled ``GradedScorecardResponse`` into three
short, role-tailored summaries — for the student, their teacher, and their parent — with
one structured Gemini call.

This lives in the app layer (not the vendored ``app/services/grader`` package) because the
three-audience shape is product-specific; it reuses the vendored call primitive
``generate_with_retry`` and keeps its prompt alongside the others in ``grader/prompts/``
(the same split used for the IB/Cambridge prompts). The call is traced in Langfuse via the
caller-supplied ``on_response`` hook, exactly like the OCR / rubric / grade calls.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.schemas.grader_schema import GradedScorecardResponse
from app.services.grader import PROMPTS_DIR
from app.services.grader.core import generate_with_retry

SUMMARIES_PROMPT = PROMPTS_DIR / "audience_summaries.txt"


class AudienceSummaries(BaseModel):
    """Three short, role-tailored summaries of one graded scorecard (Gemini output)."""

    student_summary: str = Field(
        description="2-3 sentences addressed to the STUDENT ('you'): what they did well, "
        "where marks were lost, and 1-2 concrete next steps. Encouraging, minimal jargon."
    )
    teacher_summary: str = Field(
        description="2-3 sentences for the TEACHER: overall performance level, specific "
        "strengths, and gaps/misconceptions worth reteaching. Professional and diagnostic."
    )
    parent_summary: str = Field(
        description="2-3 sentences for the PARENT in plain language (no exam/rubric jargon): "
        "how their child did, what it means, and how to support improvement at home."
    )


def build_summary_view(response: GradedScorecardResponse) -> dict[str, Any]:
    """Compact, token-light projection of the scorecard for the summaries prompt.

    Includes the overall result and a per-question (id, marks, comment) breakdown, but not
    the per-point rationale/evidence — the question comments already distill each answer.
    """
    return {
        "subject": response.subject,
        "test_name": response.test_name,
        "percentage": response.percentage,
        "total_points_earned": response.total_points_earned,
        "total_points_possible": response.total_points_possible,
        "review_flags": list(response.review_flags),
        "questions": [
            {
                "question_id": q.question_id,
                "earned": q.points_earned,
                "possible": q.points_possible,
                "comment": q.comment,
            }
            for q in response.questions
        ],
    }


def generate_audience_summaries(
    client: genai.Client,
    *,
    subject: str,
    exam_body: str | None,
    scorecard_view: dict[str, Any],
    model: str = "gemini-3.5-flash",
    on_response: Callable[..., None] | None = None,
) -> AudienceSummaries:
    """One structured Gemini call: graded scorecard -> three audience summaries.

    Mirrors ``grade_question`` (vendored ``core.py``): a system prompt + a compact user
    message, structured JSON output via ``response_schema=AudienceSummaries``, and the
    ``on_response`` Langfuse tracing hook. Raises ``RuntimeError`` if Gemini returns no
    parsed object (same contract as the grade call).
    """
    base_prompt = SUMMARIES_PROMPT.read_text(encoding="utf-8")
    view_json = json.dumps(scorecard_view, indent=2, ensure_ascii=False)
    user_message = (
        f"# Subject\n{subject}\n\n"
        f"# Qualification / exam body\n{exam_body or '(unspecified)'}\n\n"
        f"# Graded scorecard\n```json\n{view_json}\n```\n"
    )

    response = generate_with_retry(
        client,
        label="audience summaries",
        model=model,
        contents=[base_prompt, user_message],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=AudienceSummaries,
            temperature=0.3,
        ),
        on_response=on_response,
    )
    parsed = response.parsed
    if parsed is None:
        raise RuntimeError(
            f"Gemini returned no parsed AudienceSummaries; raw text:\n{response.text or '<empty>'}"
        )
    return parsed  # type: ignore[return-value]
