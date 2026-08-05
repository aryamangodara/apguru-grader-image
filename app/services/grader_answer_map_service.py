"""Reconcile typed answer keys with the rubric's question ids before grading.

Typed grading matches answers to rubric questions by an exact key lookup. When a
client keys its answers by some other numbering (e.g. ``"21".."24"`` for a rubric
whose ids are ``"216a".."220"``), that lookup finds nothing and every question scores
0 while the job still reports ``succeeded`` — a silent, misleading zero.

:func:`remap_typed_answers` closes that gap. If the submitted keys already match the
rubric it trusts them (no LLM call). Otherwise it makes one Gemini call
(:func:`map_answers_to_questions`) that matches each answer to the question(s) it
answers by content, then validates the result and **fails loud**
(:class:`AnswerMappingError`) rather than emit a plausible-but-wrong grade. On a clean
map the answers are rekeyed onto the rubric ids, the touched questions are flagged for
review, and the applied mapping is recorded on the scorecard.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import structlog
from google import genai

from app.core.config import settings
from app.core.errors import AnswerMappingError
from app.schemas.grader_schema import AppliedAnswerMap
from app.services.grader import map_answers_to_questions
from app.services.grader.core import _normalize_qid
from app.services.grader.schemas import ParsedRubric
from app.services.grader.tracing import gemini_generation_reporter

log = structlog.get_logger(__name__)


@dataclass
class RemapResult:
    """Outcome of reconciling typed answer keys with the rubric.

    ``answers_by_major`` is the (possibly rekeyed) dict to feed the grader. When
    ``remapped`` is True, ``remapped_qids`` are the rubric questions that received a
    content-mapped answer (flag them for review) and ``applied_map`` records the mapping
    for the scorecard. On the fast path (keys already matched) ``remapped`` is False and
    the answers pass through unchanged.
    """

    answers_by_major: dict[str, str]
    remapped_qids: list[str] = field(default_factory=list)
    applied_map: list[AppliedAnswerMap] = field(default_factory=list)
    remapped: bool = False


def remap_typed_answers(
    client: genai.Client,
    *,
    answers_by_major: dict[str, str],
    rubric: ParsedRubric,
) -> RemapResult:
    """Return typed answers rekeyed onto rubric question ids, content-mapping if needed.

    Fast path (no LLM call): the feature is disabled, there are no answers, or every
    submitted key is already a rubric question id — trust the keys as-is.

    Otherwise ONE Gemini call matches each answer to the rubric question(s) it answers.
    Raises :class:`AnswerMappingError` (fail loud, surfaced as a failed job) if any
    answer maps below ``grader_answer_map_min_confidence``, two answers collide on the
    same question, an answer matches nothing, or the model drops an answer. On success
    the returned ``answers_by_major`` is rekeyed to rubric ids.
    """
    rubric_ids = {_normalize_qid(q.question_id) for q in rubric.questions}
    submitted = set(answers_by_major)

    # Fast path — keys already match the rubric (or nothing to reconcile). Trust them,
    # and never pay the extra call or risk failing a submission that grades fine today.
    if not settings.grader_enable_answer_map or not submitted or submitted <= rubric_ids:
        return RemapResult(answers_by_major=answers_by_major)

    log.info(
        "answer_map_triggered",
        submitted_keys=sorted(submitted),
        rubric_id_count=len(rubric_ids),
    )
    result = map_answers_to_questions(
        client,
        answers=answers_by_major,
        rubric=rubric,
        model=settings.grader_answer_map_model,
        on_response=gemini_generation_reporter(
            "grader.answer_map", settings.grader_answer_map_model
        ),
    )

    threshold = settings.grader_answer_map_min_confidence
    by_key = {_normalize_qid(m.submitted_key): m for m in result.mappings}

    rekeyed: dict[str, str] = {}
    applied: list[AppliedAnswerMap] = []
    claimed_by: dict[str, str] = {}  # rubric qid -> the submitted key that claimed it
    problems: list[str] = []

    for key, text in answers_by_major.items():
        mapping = by_key.get(key)
        if mapping is None:
            problems.append(f"answer '{key}' was not mapped by the model")
            continue
        # Keep only ids that actually exist in the rubric (drop any the model invented).
        qids = [q for q in (_normalize_qid(x) for x in mapping.question_ids) if q in rubric_ids]
        if not qids:
            problems.append(f"answer '{key}' matched no rubric question")
            continue
        if mapping.confidence < threshold:
            problems.append(
                f"answer '{key}' mapped with confidence {mapping.confidence:.2f} < {threshold:.2f}"
            )
            continue
        collided = [q for q in qids if q in claimed_by]
        if collided:
            problems.append(
                f"rubric question {collided} claimed by both '{claimed_by[collided[0]]}' and '{key}'"
            )
            continue
        for q in qids:
            claimed_by[q] = key
            rekeyed[q] = text
        applied.append(
            AppliedAnswerMap(
                submitted_key=key, mapped_question_ids=qids, confidence=mapping.confidence
            )
        )

    if problems:
        # Fail loud: an honest failure beats a confident grade against the wrong question.
        raise AnswerMappingError(
            f"could not confidently map submitted answers to rubric questions "
            f"({len(applied)}/{len(answers_by_major)} placed): " + "; ".join(problems)
        )

    log.info(
        "answer_map_succeeded",
        mappings={a.submitted_key: a.mapped_question_ids for a in applied},
    )
    return RemapResult(
        answers_by_major=rekeyed,
        remapped_qids=sorted(rekeyed),
        applied_map=applied,
        remapped=True,
    )
