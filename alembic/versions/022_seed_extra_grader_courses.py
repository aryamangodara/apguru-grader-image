"""Seed four more AP grader courses into course_configs (id 17/21/22/31).

Adds AP Psychology (17), AP Macroeconomics (21), AP Calculus AB (22), and AP
Calculus BC (31). These exist in the canonical ``course`` table but were missing
from ``course_configs``, so the grader could not register exams for them
(``get_course_config`` raised "Unknown course_id" -> HTTP 400).

Same shape as 021: ``course_configs.id`` / ``course_id`` mirror ``course.id``;
the upsert is idempotent (``ON DUPLICATE KEY UPDATE``) so it is safe to re-run
and safe on an environment where the rows were already inserted by hand. The
placeholder NOT-NULL columns (``category`` / ``scoring_type`` / ``subjects`` /
``exam_body``) mirror the SAT/021 rows. Grading addenda are author-supplied; the
OCR addenda were written for this grader.

Revision ID: 022
Create Date: 2026-06-03
"""

import json
import re

import sqlalchemy as sa

from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


# --- grading addenda (author-supplied) --------------------------------------

_CALCULUS_GRADING = (
    "Accept algebraically equivalent forms. A sign or arithmetic error that "
    "propagates through subsequent steps should only cost the point where the "
    "error was introduced; downstream points may still be earned on follow-through."
)

_MACROECONOMICS_GRADING = (
    "Graded on correct economic reasoning, not prose quality. For graph points, "
    "require correctly labeled axes, correctly shaped/positioned curves (AD/AS, "
    "money market, loanable funds, etc.), and clearly indicated equilibria or "
    "shifts as the rubric specifies. Award an explanation point only when the "
    "response gives the correct direction of change AND the causal chain. Apply "
    "follow-through on dependent points."
)

_PSYCHOLOGY_GRADING = (
    "The 2025 redesign uses Article Analysis (AAQ) and Evidence-Based (EBQ) "
    "question formats. For each rubric point require BOTH a correct concept "
    "identification AND specific application to the scenario or article evidence. "
    "In EBQ responses, accept any citation that uniquely identifies one of the "
    "provided sources (e.g. 'Source 1', 'the second study', or the author's name)."
)

# --- OCR addenda (handwritten OCR context) ----------------------------------

_CALCULUS_OCR = (
    "For function graphs: full axis labels with scale and the curve's key features "
    "the student drew or marked - intercepts, maxima/minima, inflection points, "
    "asymptotes, and any tangent/secant lines. For area/volume sketches: the "
    "bounding curves, the shaded region, and any representative rectangle/disk/shell "
    "with labelled dimensions. For slope fields: the slope direction of each segment "
    "at its grid point. For sign charts/number lines: the critical values and the "
    "sign (+/-) on each interval. For tables of values: every header and cell. "
    "Transcribe notation faithfully - integral bounds, derivative/limit notation, "
    "and units."
)

_MACROECONOMICS_OCR = (
    "Answers are graph-heavy; reproduce each diagram precisely (AD/AS, money market, "
    "loanable funds, Phillips curve, PPC, foreign-exchange market, bank T-account): "
    "axis labels with their exact variables (e.g. price level vs real GDP; nominal "
    "interest rate vs quantity of money; real interest rate vs quantity of loanable "
    "funds), every curve the student labelled (AD, SRAS, LRAS, MS, MD, etc.), the "
    "direction of any shift (arrow plus from/to), and the old/new equilibria marked. "
    "For T-accounts transcribe each side's entries and amounts; transcribe any "
    "calculation (money multiplier, GDP, etc.) with setup and result."
)

_PSYCHOLOGY_OCR = (
    "AAQ/EBQ responses are prose, not diagrams - transcribe the written answer "
    "verbatim, preserving labelled parts (A, B, C, ...). Capture any source "
    "citations used to attribute evidence (e.g. 'Source 1', 'the second study', an "
    "author's name), since rubric points depend on them. Diagrams aren't expected; "
    "if the student sketches one, describe it briefly."
)


# course_configs.id (== course.id) -> course_name (verbatim from the `course` table)
COURSES: dict[int, str] = {
    17: "AP Psychology",
    21: "AP Macroeconomics",
    22: "AP Calculus AB",
    31: "AP Calculus BC",
}

GRADING_ADDENDA: dict[int, str] = {
    17: _PSYCHOLOGY_GRADING,
    21: _MACROECONOMICS_GRADING,
    22: _CALCULUS_GRADING,
    31: _CALCULUS_GRADING,
}

OCR_ADDENDA: dict[int, str] = {
    17: _PSYCHOLOGY_OCR,
    21: _MACROECONOMICS_OCR,
    22: _CALCULUS_OCR,
    31: _CALCULUS_OCR,
}

# Placeholders for required NOT-NULL columns the grader doesn't use; mirror the
# SAT/021 rows so the constraints pass and the grader can read the row.
_EXAM_BODY = "College Board"
_CATEGORY = "prep"
_SCORING_TYPE = "composite"

_slug_re = re.compile(r"[^a-z0-9]+")


def _subject_slug(course_name: str) -> str:
    """Lowercase hyphen slug of the course name (drops a leading 'AP '). Mirrors 021."""
    name = course_name.strip()
    if name.lower().startswith("ap "):
        name = name[3:]
    return _slug_re.sub("-", name.lower()).strip("-")


def upgrade() -> None:
    conn = op.get_bind()
    stmt = sa.text(
        "INSERT INTO course_configs "
        "(id, course_id, course_name, exam_body, category, scoring_type, subjects, "
        " is_active, grading_addendum, ocr_addendum) "
        "VALUES (:id, :course_id, :course_name, :exam_body, :category, :scoring_type, "
        " :subjects, 1, :g, :o) "
        "ON DUPLICATE KEY UPDATE "
        " course_name=VALUES(course_name), exam_body=VALUES(exam_body), "
        " category=VALUES(category), scoring_type=VALUES(scoring_type), "
        " subjects=VALUES(subjects), is_active=VALUES(is_active), "
        " grading_addendum=VALUES(grading_addendum), ocr_addendum=VALUES(ocr_addendum)"
    )
    for cid, name in COURSES.items():
        conn.execute(
            stmt,
            {
                "id": cid,
                "course_id": str(cid),
                "course_name": name,
                "exam_body": _EXAM_BODY,
                "category": _CATEGORY,
                "scoring_type": _SCORING_TYPE,
                "subjects": json.dumps([_subject_slug(name)]),
                "g": GRADING_ADDENDA.get(cid, ""),
                "o": OCR_ADDENDA.get(cid, ""),
            },
        )


def downgrade() -> None:
    conn = op.get_bind()
    stmt = sa.text("DELETE FROM course_configs WHERE id = :id")
    for cid in COURSES:
        conn.execute(stmt, {"id": cid})
