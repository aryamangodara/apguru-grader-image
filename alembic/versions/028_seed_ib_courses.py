"""Seed the IB course catalog rows (ids 39-60, 113-117) into course_configs, with addenda.

Adds International Baccalaureate courses so the AP FRQ grader can grade IB exams
too. Mirrors migration 021 exactly: **INSERT** (upsert via
``ON DUPLICATE KEY UPDATE``, keyed on the PK ``id``), with ``course_id = str(id)``.
Every row carries ``exam_body = 'IBO'`` — the flag that makes ``register_exam`` /
``_do_grade`` select the IB rubric/grade prompt variants
(``app/services/grader_prompts.py``) instead of the AP ones. The grader reads each
row via ``get_course_config(course_id)`` (which filters ``is_active = 1``), so after
running, restart the app (or call ``clear_course_config_cache()``) for the
permanently cached configs to pick the new rows up.

IB marks two ways and the addenda reflect it:
- **Point-based** (Maths, Physics, Chemistry, Biology, Computer Science, ESS) gets
  AP-science-style grading guidance (method/accuracy/reasoning marks, ECF) and, for
  the diagram-heavy sciences, an OCR addendum.
- **Markband** (Business Management, Economics, Geography, History, Global Politics,
  Psychology, Philosophy) gets level-descriptor guidance: each rubric point is an
  assessment criterion scored by best-fit band, with partial credit — see
  ``grade_question_ib.txt``. Economics also gets a diagram OCR addendum.

The required NOT-NULL columns the grader doesn't read (``category``,
``scoring_type``, ``subjects``) carry honest IB placeholders; refine them if these
courses are later wired into scoring/tutor features.

Revision ID: 028
Create Date: 2026-06-17
"""

import json
import re

import sqlalchemy as sa

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Grading addenda — point-based subjects (mark-by-mark, like AP sciences/maths)
# ---------------------------------------------------------------------------

_MATH_GRADING = (
    "IB Mathematics is marked with method (M), accuracy (A) and reasoning (R) marks. "
    "Award M marks for a valid, clearly indicated method even if it contains an "
    "arithmetic slip; award A marks only for correct results, and an A mark depends "
    "on the preceding M mark. Apply ECF / follow-through: a value that is wrong "
    "because of an earlier error but is then used with correct subsequent method "
    "still earns the later M (and dependent A) marks. Accept equivalent forms (exact "
    "or correctly rounded decimals, algebraically equivalent expressions) and correct "
    "alternative valid methods. Where the answer is given (AG), the working must "
    "justify it; a bare answer with no working earns only what the scheme allows for "
    "the answer alone."
)

_PHYSICS_GRADING = (
    "IB Physics is point-marked (relationship/setup, substitution, final answer with "
    "units, plus explanation marks). Apply ECF / follow-through so an incorrect "
    "earlier value used correctly downstream still earns later marks. Accept "
    "algebraically equivalent expressions and correct alternative methods; require "
    "correct units, significant figures within the scheme's tolerance where it "
    "specifies, and vector direction where relevant. Award explanation marks only for "
    "a correct, coherent line of physics reasoning linked to the scenario — a bare "
    "claim or an unexplained equation does not earn it. 'OWTTE' (or words to that "
    "effect) means accept any wording carrying the same physics."
)

_CHEMISTRY_GRADING = (
    "IB Chemistry is point-marked. For calculations require the correct setup AND the "
    "final answer with appropriate units, applying ECF so an incorrect earlier value "
    "used correctly downstream still earns later marks; accept answers within the "
    "scheme's tolerance and penalize significant figures only where the scheme says "
    "so. Require balanced equations, correct formulae and state symbols where "
    "specified. Explanation marks require correct particulate-/molecular-level "
    "reasoning (bonding, intermolecular forces, electronegativity, collision theory, "
    "equilibrium shifts) tied to the prompt — a correct answer with no valid "
    "reasoning earns no reasoning mark."
)

_BIOLOGY_GRADING = (
    "IB Biology is point-marked; award each mark independently for the specific idea "
    "the scheme credits (accept 'OWTTE' wording). 'Explain' / 'suggest' marks require "
    "a stated mechanism at the molecular, cellular, or ecological level tied to the "
    "prompt — a correct conclusion with no valid reasoning earns no reasoning mark. "
    "For data-analysis parts require a trend stated with reference to the data "
    "(manipulated and responding variables), not a bare restatement of figures; for "
    "calculations require the working and correct units. Apply ECF on dependent steps."
)

_CS_GRADING = (
    "IB Computer Science answers are marked for correct algorithmic intent. Accept "
    "pseudocode or Java that is functionally correct even with minor syntax slips "
    "(missing semicolons, approximate variable names) unless the scheme explicitly "
    "requires strict syntax; variable-name differences are not penalized. Award marks "
    "for the correct construct (loop, condition, method call, correct use of a data "
    "structure) and correct logic/order; deduct only where the logic is wrong or a "
    "required step is missing. For trace/output questions require the correct final "
    "result, applying follow-through on an earlier consistent slip."
)

_ESS_GRADING = (
    "IB Environmental Systems & Societies is point-marked and rewards specificity. "
    "Vague or generic statements earn no credit — require a concrete mechanism, named "
    "example, or specific cause-and-effect link relevant to the systems/sustainability "
    "context. For calculation parts require the setup/working and correct units, "
    "applying follow-through on arithmetic. 'Explain/evaluate' demands developed "
    "reasoning (and, for evaluate, more than one perspective); 'identify/state' may be "
    "brief. Where a part is marked by a level band, apply best-fit markband judgement."
)


# ---------------------------------------------------------------------------
# Grading addenda — markband subjects (level descriptors, best-fit, partial credit)
# ---------------------------------------------------------------------------

_MARKBAND_BASE = (
    "This subject's extended responses are marked with IB markbands (level "
    "descriptors), not independent points. Each rubric point whose criterion lists "
    "multiple mark bands is one assessment criterion: read the whole response, choose "
    "by best-fit the single band whose descriptor it matches, and award a specific "
    "mark within that band (lower/middle/upper) reflecting fit — do NOT award the full "
    "marks merely for reaching the band, and never combine bands. Short "
    "'define/identify/outline' parts that are point-marked are awarded all-or-nothing "
    "as usual. Credit substance over length; reward a sustained, well-evidenced "
    "argument that addresses the command term. "
)

_BUSINESS_GRADING = _MARKBAND_BASE + (
    "Business Management rewards application of specific business tools/theory to the "
    "stimulus organization, balanced analysis, and — for 'evaluate/examine/recommend' "
    "— a substantiated judgement; generic theory not tied to the case earns little."
)

_ECONOMICS_GRADING = _MARKBAND_BASE + (
    "Economics rewards accurate definitions of key terms, correctly drawn and fully "
    "labelled diagrams, application to the context, and — for 'evaluate/discuss' — "
    "reasoned evaluation weighing more than one viewpoint; a correct diagram with no "
    "explanation, or evaluation with no economic theory, is limited."
)

_GEOGRAPHY_GRADING = _MARKBAND_BASE + (
    "Geography rewards accurate geographic terminology, support from located "
    "examples/case studies and any provided resources, and — for 'examine/evaluate/"
    "discuss' — a structured, balanced argument that reaches a conclusion."
)

_HISTORY_GRADING = _MARKBAND_BASE + (
    "History rewards accurate, relevant own knowledge, focus on the demands of the "
    "question, analysis over narrative, and — for essays — a balanced argument with a "
    "substantiated judgement; for source questions, value/limitation must be grounded "
    "in origin, purpose and content."
)

_GLOBAL_POLITICS_GRADING = _MARKBAND_BASE + (
    "Global Politics rewards use of political concepts, real-world examples/case "
    "studies, consideration of different perspectives and levels of analysis, and an "
    "explicit, justified conclusion for 'evaluate/to what extent' prompts."
)

_PSYCHOLOGY_GRADING = _MARKBAND_BASE + (
    "Psychology rewards accurate use of relevant theories/studies, explicit links to "
    "the question, and critical thinking (methodology, comparisons, limitations, "
    "applications) for ERQs; SAQs require an accurate, focused explanation with a "
    "relevant study."
)

_PHILOSOPHY_GRADING = _MARKBAND_BASE + (
    "Philosophy rewards a clear grasp of the philosophical issue, a well-structured "
    "argument with reasons, use of relevant philosophical material, and critical "
    "evaluation including counter-arguments."
)


# ---------------------------------------------------------------------------
# OCR addenda — diagram-heavy subjects (student draws diagrams in the answer)
# ---------------------------------------------------------------------------

_MATH_OCR = (
    "Transcribe mathematical working faithfully: every line of algebra, each step's "
    "operator, and the final answer (LaTeX between $...$). For graphs/sketches: axis "
    "labels with scale, the curve's shape and key features (intercepts, turning "
    "points, asymptotes), and any points or regions the student marked or shaded. For "
    "geometry/vector/probability diagrams: labelled lengths and angles, vector "
    "directions and magnitudes, and branch labels/probabilities."
)

_PHYSICS_OCR = (
    "For free-body / force diagrams: every vector with its tail point, head direction "
    "(up/down/left/right or angle), labelled magnitude (mg, N, T, f, F_app, etc.), and "
    "the object it acts on. For circuit diagrams: each component (resistor, capacitor, "
    "cell, switch) with its labelled value, the connection topology, and any labelled "
    "current direction or polarity. For field / ray diagrams: arrow directions, "
    "relative density, labelled magnitudes, lens/mirror type, focal points and image "
    "position. For motion / P-V / graph sketches: coordinate axes with labels, units "
    "and scale, the curve shape (linear, parabolic, constant), key values at labelled "
    "points, and any area the student shaded."
)

_CHEMISTRY_OCR = (
    "Diagram fidelity matters — marks are awarded for specific bonds, lone pairs, "
    "charges and geometries the student drew. For Lewis / dot structures: name every "
    "atom by element symbol, every bond by its multiplicity and the two atoms it joins "
    "(e.g. 'C=O', 'N-H'), every lone pair (count and on which atom), every formal "
    "charge with sign and adjacent atom, and the overall shape (bent, trigonal planar, "
    "tetrahedral, octahedral) when discernible. For intermolecular-force diagrams: for "
    "each interaction line drawn, state the donor atom (and which H), the acceptor atom "
    "(and which lone pair), the molecule each belongs to, and its position on the page. "
    "For energy / reaction-profile diagrams: each peak's relative height, the position "
    "and label of any intermediate, and whether reactants are higher or lower than "
    "products. For graphs/titration curves: axes, scale, and every plotted feature "
    "including equivalence points."
)

_BIOLOGY_OCR = (
    "For biological diagrams (cell, organelle, tissue, organ, organism): name every "
    "structure the student labelled and where it sits relative to others. For cycle "
    "diagrams (Krebs, Calvin, cell cycle, nitrogen cycle): name each stage in order, "
    "the direction of every arrow, and any inputs/outputs written on the arrows. For "
    "experimental graphs: axes (label + units + scale), trend per group/treatment, "
    "error bars or ranges if drawn, and any annotation the student added (significance "
    "markers, labelled controls vs experimentals). For pedigrees, Punnett squares and "
    "gel images: the row/column layout and what is in each cell."
)

_ESS_OCR = (
    "For data tables: reproduce the row and column headers (with units) and every cell "
    "value the student wrote. For graphs: axis labels with units and scale, the trend "
    "per series, and any value the student read off or annotated. For cycle / system "
    "diagrams (carbon, nitrogen, water, energy flow): name each labelled pool or stage "
    "and the direction of every arrow, with any flux value written on it. For "
    "calculation work: transcribe the setup, the units, and the final value verbatim."
)

_ECONOMICS_OCR = (
    "Economics answers rely on diagrams — transcribe them precisely. For "
    "supply-and-demand / cost-and-revenue / AD-AS diagrams: label both axes (with units "
    "where given), name every curve drawn (e.g. S, D, MC, ATC, AD, SRAS), the direction "
    "of any shift and its new position, each equilibrium point and any price/quantity "
    "lines dropped to the axes, and any area the student shaded or labelled (welfare "
    "loss, tax revenue, surplus). State which curves moved and to where. Also "
    "transcribe any calculation working and the final value with units (%, $, etc.)."
)


# ---------------------------------------------------------------------------
# course_configs.id -> addenda
# ---------------------------------------------------------------------------

GRADING_ADDENDA: dict[int, str] = {
    39: _MATH_GRADING,             # IB Math AA SL
    40: _MATH_GRADING,             # IB Math AA HL
    41: _MATH_GRADING,             # IB Math AI HL
    42: _MATH_GRADING,             # IB Math AI SL
    45: _PHYSICS_GRADING,          # IB Physics HL
    46: _CHEMISTRY_GRADING,        # IB Chemistry HL
    47: _PHYSICS_GRADING,          # IB Physics SL
    48: _CHEMISTRY_GRADING,        # IB Chemistry SL
    49: _BIOLOGY_GRADING,          # IB Biology HL
    50: _BIOLOGY_GRADING,          # IB Biology SL
    51: _BUSINESS_GRADING,         # IB Business Management
    52: _ECONOMICS_GRADING,        # IB Economics
    53: _GEOGRAPHY_GRADING,        # IB Geography
    54: _HISTORY_GRADING,          # IB History
    55: _GLOBAL_POLITICS_GRADING,  # IB Global Politics
    56: _PSYCHOLOGY_GRADING,       # IB Psychology HL
    57: _PSYCHOLOGY_GRADING,       # IB Psychology SL
    58: _PHILOSOPHY_GRADING,       # IB Philosophy
    59: _CS_GRADING,               # IB Computer Science
    60: _ESS_GRADING,              # IB EVS (Environmental Systems & Societies)
    113: _ECONOMICS_GRADING,       # IB Economics HL
    114: _ECONOMICS_GRADING,       # IB Economics SL
    116: _BUSINESS_GRADING,        # IB Business Management HL
    117: _BUSINESS_GRADING,        # IB Business Management SL
}

OCR_ADDENDA: dict[int, str] = {
    39: _MATH_OCR,        # IB Math AA SL
    40: _MATH_OCR,        # IB Math AA HL
    41: _MATH_OCR,        # IB Math AI HL
    42: _MATH_OCR,        # IB Math AI SL
    45: _PHYSICS_OCR,     # IB Physics HL
    46: _CHEMISTRY_OCR,   # IB Chemistry HL
    47: _PHYSICS_OCR,     # IB Physics SL
    48: _CHEMISTRY_OCR,   # IB Chemistry SL
    49: _BIOLOGY_OCR,     # IB Biology HL
    50: _BIOLOGY_OCR,     # IB Biology SL
    52: _ECONOMICS_OCR,   # IB Economics
    60: _ESS_OCR,         # IB EVS
    113: _ECONOMICS_OCR,  # IB Economics HL
    114: _ECONOMICS_OCR,  # IB Economics SL
}


# course_configs.id -> course_name (verbatim from the supplied CSV, trimmed). These
# rows are INSERTed because the grader's catalog was missing them.
COURSES: dict[int, str] = {
    39: "IB Math AA SL",
    40: "IB Math AA HL",
    41: "IB Math AI HL",
    42: "IB Math AI SL",
    45: "IB Physics HL",
    46: "IB Chemistry HL",
    47: "IB Physics SL",
    48: "IB Chemistry SL",
    49: "IB Biology HL",
    50: "IB Biology SL",
    51: "IB Business Management",
    52: "IB Economics",
    53: "IB Geography",
    54: "IB History",
    55: "IB Global Politics",
    56: "IB Psychology HL",
    57: "IB Psychology SL",
    58: "IB Philosophy",
    59: "IB Computer Science",
    60: "IB EVS",
    113: "IB Economics HL",
    114: "IB Economics SL",
    116: "IB Business Management HL",
    117: "IB Business Management SL",
}

# exam_body = "IBO" is the live flag the grader reads (selects the IB prompts).
# category / scoring_type / subjects are required NOT-NULL columns the grader does
# not read; honest IB placeholders here.
_EXAM_BODY = "IBO"
_CATEGORY = "academic"
_SCORING_TYPE = "grade"

_slug_re = re.compile(r"[^a-z0-9]+")


def _subject_slug(course_name: str) -> str:
    """Lowercase hyphen slug of the course name (drops a leading 'IB ')."""
    name = course_name.strip()
    if name.lower().startswith("ib "):
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
    params = [
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
        }
        for cid, name in COURSES.items()
    ]
    conn.execute(stmt, params)  # one batched executemany roundtrip


def downgrade() -> None:
    conn = op.get_bind()
    # Single batched delete via an expanding bound param — named params, not an
    # f-string IN clause (CLAUDE.md: "named SQL parameters only — never f-strings").
    stmt = sa.text("DELETE FROM course_configs WHERE id IN :ids").bindparams(
        sa.bindparam("ids", expanding=True)
    )
    conn.execute(stmt, {"ids": list(COURSES)})
