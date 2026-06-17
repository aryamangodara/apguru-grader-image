"""Compose the grader's Scorecard + rubric + submission into the UI-complete JSON.

The JSON analogue of the grader's ``render_html_report``: it surfaces every
component the HTML scorecard shows (per-question transcript, status tags, source
pages, OCR confidence; per-point criterion, rationale, evidence, confidence) so
the frontend can render the card from JSON alone.
"""
from __future__ import annotations

import re

from app.schemas.grader_schema import (
    GradedPoint,
    GradedQuestion,
    GradedScorecardResponse,
    QuestionMarks,
)

from .core import _looks_like_subpart, _normalize_qid, flatten_rubric_by_subpart
from .schemas import ParsedRubric, ParsedSubmission, Scorecard, TranscribedAnswer


def _major_qid(qid: str) -> str:
    """Major question for a (sub-part) qid: '1a'->'1', '3c-ii'->'3', '5'->'5', 'frq-3a'->'frq-3'.

    Matches up to and including the first digit run, so a non-numeric prefix
    ('frq-3a') still rolls up to its major ('frq-3'). Falls back to the normalized
    qid when there is no digit at all (e.g. 'intro').
    """
    normalized = _normalize_qid(qid)
    match = re.match(r"^(.*?\d+)", normalized)
    return match.group(1) if match else normalized


def build_question_wise_marks(questions: list[GradedQuestion]) -> list[QuestionMarks]:
    """Earned marks per major question — sub-parts summed, numeric-sorted.

    Aggregates ``points_earned`` across the graded + unattempted ``GradedQuestion``s
    so each major question maps to a single number (a flat, directly-parseable shape),
    regardless of whether the rubric was scored at major or sub-part granularity. The
    summed marks equal the scorecard's ``total_points_earned``.
    """
    totals: dict[str, float] = {}
    for question in questions:
        major = _major_qid(question.question_id)
        totals[major] = totals.get(major, 0.0) + question.points_earned

    def _sort_key(qid: str) -> list[str | int]:
        # Natural sort: split into text/number runs, numbers compared as ints. Pure
        # numeric ids sort first (their leading split element is the empty string).
        return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", qid)]

    return [
        QuestionMarks(question_id=qid, marks=round(totals[qid], 2))
        for qid in sorted(totals, key=_sort_key)
    ]


def _resolve_answer(
    qid: str,
    answer_by_qid: dict[str, TranscribedAnswer],
) -> TranscribedAnswer | None:
    """Find the transcribed answer backing a graded qid.

    Direct hit first; otherwise treat ``qid`` as a recovered sub-part and fall
    back to its most-specific parent (mirrors the placement rule in
    ``render_html_report``) so handwritten sub-parts inherit the parent's pages.
    """
    direct = answer_by_qid.get(qid)
    if direct is not None:
        return direct
    for cand in sorted(answer_by_qid, key=len, reverse=True):
        if _looks_like_subpart(qid, cand):
            return answer_by_qid[cand]
    return None


def build_scorecard_response(
    scorecard: Scorecard,
    rubric: ParsedRubric,
    submission: ParsedSubmission,
    *,
    test_id: int,
    test_name: str | None = None,
    is_handwritten: bool,
    recovered_qids: list[str] | None = None,
    merged_parent_answers: dict[str, TranscribedAnswer] | None = None,
    missing_qids: list[str] | None = None,
    ai_labelled_qids: list[str] | None = None,
    low_confidence_threshold: float = 0.75,
    answers_pdf_url: str | None = None,
    page_count: int | None = None,
) -> GradedScorecardResponse:
    """Assemble the frontend-ready scorecard from the grader's outputs."""
    recovered = set(recovered_qids or [])
    merged = set(merged_parent_answers or {})
    missing = set(missing_qids or [])
    ai_labelled = set(ai_labelled_qids or [])

    # point_id -> RubricPoint (for criterion + per-point max)
    point_by_id = {p.point_id: p for q in rubric.questions for p in q.rubric_points}
    # sub-part qid -> prompt summary
    prompt_by_qid = {
        qid: qr.prompt_summary for qid, qr in flatten_rubric_by_subpart(rubric).items()
    }
    # answers available for transcript / pages / confidence (originals + merged parents)
    answer_by_qid: dict[str, TranscribedAnswer] = {
        a.question_id: a for a in submission.answers
    }
    answer_by_qid.update(merged_parent_answers or {})

    questions: list[GradedQuestion] = []
    unattempted: list[GradedQuestion] = []

    for qs in scorecard.questions:
        qid = qs.question_id

        if qid in missing:
            status = "unattempted"
        elif qid in merged:
            status = "merged"
        elif qid in recovered:
            status = "recovered"
        else:
            status = "graded"

        ans = _resolve_answer(qid, answer_by_qid)
        ocr_confidence = (
            None if not is_handwritten else (ans.confidence if ans else None)
        )
        low_conf = ocr_confidence is not None and ocr_confidence < low_confidence_threshold
        source_pages = list(ans.source_pages) if ans else []

        tags: list[str] = []
        if low_conf:
            tags.append("low OCR")
        if status == "merged":
            tags.append("merged from sub-parts")
        elif status == "recovered":
            tags.append("shared transcript")
        elif status == "unattempted":
            tags.append("unattempted")
        if qid in ai_labelled:
            tags.append("AI-labelled")

        points = [
            GradedPoint(
                point_id=ps.point_id,
                criterion=(
                    point_by_id[ps.point_id].criterion
                    if ps.point_id in point_by_id
                    else None
                ),
                awarded=ps.awarded,
                points_earned=ps.points_earned,
                points_possible=(
                    point_by_id[ps.point_id].point_value
                    if ps.point_id in point_by_id
                    else ps.points_earned
                ),
                rationale=ps.rationale,
                transcript_evidence=ps.transcript_evidence,
                grading_confidence=ps.grading_confidence,
                review_recommended=ps.review_recommended,
            )
            for ps in qs.point_scores
        ]

        gq = GradedQuestion(
            question_id=qid,
            prompt_summary=prompt_by_qid.get(qid),
            comment=qs.summary_comment,
            points_earned=qs.points_earned,
            points_possible=qs.points_possible,
            status=status,
            transcript=qs.transcript_used or (ans.transcript if ans else ""),
            ocr_confidence=ocr_confidence,
            low_confidence=low_conf,
            source_pages=source_pages,
            tags=tags,
            points=points,
        )
        (unattempted if status == "unattempted" else questions).append(gq)

    return GradedScorecardResponse(
        test_id=test_id,
        subject=scorecard.subject,
        test_name=test_name,
        generated_at=scorecard.generated_at,
        percentage=scorecard.percentage,
        total_points_earned=scorecard.total_points_earned,
        total_points_possible=scorecard.total_points_possible,
        question_wise_marks=build_question_wise_marks(questions + unattempted),
        questions_graded=len(questions),
        review_flags=list(scorecard.review_flags),
        is_handwritten=is_handwritten,
        answers_pdf_url=answers_pdf_url,
        page_count=page_count,
        questions=questions,
        unattempted=unattempted,
    )
