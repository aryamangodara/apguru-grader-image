"""Seed the AP course catalog rows (id 14-36) into course_configs, with addenda.

The target DB's ``course_configs`` was missing these courses, so this migration
**INSERTs** them (upsert via ``ON DUPLICATE KEY UPDATE``, keyed on the PK ``id``
from the supplied id/name CSV). ``course_id`` is set = ``str(id)`` to match the
existing convention (the SAT row has id=1 / course_id="1"). Each row carries the
AP FRQ grader's ``grading_addendum`` / ``ocr_addendum`` (added as columns by 019)
so the grader has subject-specific guidance; the grader reads them via
``get_course_config(course_id)`` (which filters ``is_active=1`` — rows are
inserted active). After running, restart the app (or call
``clear_course_config_cache()``) so the permanently cached configs pick it up.

Addenda for subjects already in the grader's ``config.py`` are copied verbatim;
the rest (European/US History, Physics 1/2, English Literature, US Government, +
Physics 1/2 & Environmental Science OCR) are authored here from the official
College Board rubric structure. The required NOT-NULL columns the grader doesn't
use (``category``, ``scoring_type``, ``subjects``) are **placeholders mirroring
the SAT row** — refine them if these courses are wired into other features.

NOTE: if an earlier (UPDATE-only) version of 021 was already applied as a no-op,
run ``alembic downgrade 020`` then ``alembic upgrade head`` so this INSERT runs.

Revision ID: 021
Create Date: 2026-06-02
"""

import json
import re

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


# ---------------------------------------------------------------------------
# Newly authored grading addenda (subjects absent from the grader's config.py)
# ---------------------------------------------------------------------------

# AP European History & AP United States History share the College Board AP
# History DBQ/LEQ rubric, so both use this text.
_HISTORY_DBQ_LEQ = (
    "Apply the College Board AP History DBQ/LEQ rubric, scoring each row "
    "independently — do not require a flawless essay. Thesis/Claim (1): a "
    "historically defensible claim that responds to all parts of the prompt and "
    "establishes a line of reasoning, not a restatement of the prompt. "
    "Contextualization (1): a broader historical situation relevant to the prompt. "
    "Evidence (DBQ up to 3 / LEQ up to 2): for DBQs, accurately use the content of "
    "the required number of documents to support an argument, and for the top point "
    "explain how the point of view, purpose, audience, or historical situation of "
    "the required number of documents is relevant; for LEQs, cite specific relevant "
    "outside historical evidence. Analysis & Reasoning (2): use a reasoning process "
    "(causation, comparison, or continuity and change) to frame or structure the "
    "argument, and demonstrate complex understanding (nuance, corroboration or "
    "qualification, or multiple variables). Apply follow-through where a later row "
    "builds on an earlier defensible claim."
)

_PHYSICS1_GRADING = (
    "Algebra-based physics. Award points per the rubric's structure (correct "
    "relationship/setup, substitution of values, final answer with units). Apply "
    "follow-through: an incorrect earlier value used correctly downstream still "
    "earns the later points. Accept algebraically equivalent expressions and "
    "correct symbolic answers; require correct units and vector direction where the "
    "rubric specifies. For the paragraph-length / qualitative-reasoning part, award "
    "the point only for a clear, correct, and coherent line of reasoning that "
    "connects physics principles to the scenario — a bare claim or an unexplained "
    "formula does not earn it. Do not award an answer point for a bare numerical "
    "result when the rubric requires supporting work."
)

_PHYSICS2_GRADING = (
    "Algebra-based physics (fluids, thermodynamics, electricity & magnetism, "
    "optics, and modern physics). Award points per the rubric's structure (correct "
    "relationship/setup, substitution, final answer with units), with follow-through "
    "on dependent steps so an incorrect earlier value used correctly downstream "
    "still earns later points. Accept algebraically equivalent and correct symbolic "
    "answers; require units and direction where the rubric specifies. For "
    "paragraph-length / qualitative-reasoning parts, require a correct and coherent "
    "explanation linking physics principles to the scenario, not a bare claim or an "
    "unexplained formula."
)

_ENGLISH_LITERATURE_GRADING = (
    "Apply the 6-point analytic rubric for literary analysis (poetry, prose, and "
    "the open/Q3 essay): Thesis (1 pt — a defensible interpretation that responds to "
    "the prompt, not mere observation or plot summary); Evidence & Commentary (up to "
    "4 pts — specific textual references plus commentary that explains how they "
    "support the line of reasoning and relate to the writer's choices and meaning); "
    "and Sophistication (1 pt — genuine complexity or nuance of argument or style, "
    "awarded sparingly). Reward a defensible interpretation and specific, "
    "well-explained textual support; do not penalize minor grammar or spelling "
    "unless it obscures meaning, and do not require coverage of every device."
)

_US_GOVERNMENT_GRADING = (
    "AP U.S. Government & Politics FRQs are point-based across four task types; "
    "apply the one matching the prompt. Concept Application (3 pts): define/describe "
    "the concept, then apply it to the scenario and to a political outcome. "
    "Quantitative Analysis (4 pts): identify data from the visual, describe a "
    "pattern or trend, draw a conclusion, and explain how it relates to a political "
    "principle, institution, behavior, or process. SCOTUS Comparison (4 pts): "
    "identify the constitutional clause or principle common to the cited and the "
    "non-cited case, explain how the facts/reasoning connect them, and explain the "
    "relevance. Argument Essay (6 pts): a defensible claim/thesis with a line of "
    "reasoning; at least two pieces of accurate, relevant evidence including at "
    "least one required foundational document; reasoning that explains how the "
    "evidence supports the claim; and a response to an alternative or opposing "
    "perspective via refutation, concession, or rebuttal. Require specific, accurate "
    "application; generic restatement earns no application credit."
)


# ---------------------------------------------------------------------------
# Newly authored OCR addenda (diagram-heavy subjects absent from config.py)
# ---------------------------------------------------------------------------

_PHYSICS1_OCR = (
    "For free-body / force diagrams: every vector with its tail point, head "
    "direction (up/down/left/right or angle), labelled magnitude (mg, N, T, f, "
    "F_app, etc.), and the object it acts on. For motion / kinematics sketches: "
    "coordinate axes and position-time / velocity-time / acceleration-time curves "
    "with their shape (linear, parabolic, constant) and key values at labelled "
    "points. For simple DC circuit sketches: each component with its labelled value "
    "and the connection topology. For graphs generally: full axis labels with units, "
    "scale, every plotted point or curve, and any slope or area the student marked."
)

_PHYSICS2_OCR = (
    "For circuit diagrams: each component (resistor, capacitor, battery, switch) "
    "with its labelled value, the connection topology, and any labelled current "
    "direction or polarity. For field diagrams (electric / magnetic): arrow "
    "directions, relative arrow density, labelled magnitudes, and any Gaussian "
    "surface drawn. For ray / optics diagrams: each ray's direction, the lens or "
    "mirror type and the principal axis, focal points, and the image location and "
    "orientation if drawn. For thermodynamics: P-V diagrams with labelled axes and "
    "units, each state point and the process paths between them, plus any area the "
    "student shaded. For graphs generally: axes, scale, and every plotted feature "
    "including discontinuities."
)

_ENVIRONMENTAL_SCIENCE_OCR = (
    "For data tables: reproduce the row and column headers (with units) and every "
    "cell value the student wrote. For graphs: axis labels with units and scale, the "
    "trend per series, and any value the student read off or annotated. For cycle / "
    "system diagrams (carbon, nitrogen, water, energy flow): name each labelled pool "
    "or stage and the direction of every arrow, with any flux value the student "
    "wrote on it. For calculation work: transcribe the setup, the units, and the "
    "final value verbatim."
)


# ---------------------------------------------------------------------------
# Verbatim copies from the grader's config.py (subjects already supported there)
# ---------------------------------------------------------------------------

_BIOLOGY_GRADING = (
    "FRQs are point-based; award each point independently. For quantitative "
    "parts (typically Q1 / statistical-analysis), require the correct setup "
    "AND the numerical answer with appropriate units; accept answers within "
    "the rubric's stated tolerance and do not over-penalize significant "
    "figures. Apply follow-through (consequent) credit: an incorrect earlier "
    "value used correctly downstream still earns the later points. "
    "Justify / Explain / Predict points require a stated mechanism at the "
    "molecular, cellular, or organismal level (e.g. enzyme structure-function, "
    "membrane transport, signal transduction, natural selection on heritable "
    "variation) tied to the prompt — a correct conclusion with no valid "
    "reasoning earns no reasoning point. For experimental-design parts, "
    "require a testable hypothesis tied to the independent and dependent "
    "variables, an explicit control, and a justification for replication or "
    "sample size where the rubric specifies. Graph points require correctly "
    "labeled axes with units, an appropriate scale, and data plotted "
    "accurately; 'describe the data' demands a trend tied to the variables, "
    "not a bare numerical restatement."
)

_STATISTICS_GRADING = (
    "FRQs are scored holistically per part: communication and reasoning carry as "
    "much weight as the numerical answer. Award full credit only when the response "
    "is stated in context (named variable, population, and units where appropriate), "
    "not just symbolically. For inference questions, require all four components — "
    "defined parameter and hypotheses, conditions checked, mechanics (test statistic "
    "and p-value, or the interval), and a conclusion linked to the significance level "
    "and the original claim. Apply follow-through: an incorrect earlier value used "
    "correctly downstream still earns the later points. Graph points require correctly "
    "labeled axes and scale plus a shape/center/spread description tied to the "
    "scenario; a bare numerical answer with no justification does not earn the "
    "reasoning point."
)

_CHEMISTRY_GRADING = (
    "FRQs are point-based; award each point independently. For calculations, "
    "require the correct setup AND the final answer with appropriate units, and "
    "apply follow-through (consequent) credit so an incorrect earlier value used "
    "correctly downstream still earns later points. Accept answers within the "
    "rubric's stated tolerance and do not over-penalize significant figures. "
    "Explanation/justification points require correct particulate- or "
    "molecular-level reasoning (intermolecular forces, Coulombic attraction, "
    "collision theory, etc.) tied to the prompt — a correct answer with no valid "
    "reasoning earns no reasoning point. Require balanced equations and correct "
    "chemical formulas where the rubric specifies."
)

_PRECALCULUS_GRADING = (
    "Accept algebraically equivalent forms and equivalent exact or decimal answers. "
    "Apply follow-through: an arithmetic or sign error costs only the point where it "
    "occurs, and downstream points may still be earned. Where the rubric asks for "
    "justification or reasoning, a bare final answer does not earn the reasoning point."
)

_HUMAN_GEOGRAPHY_GRADING = (
    "Require a concept definition paired with concrete application to the stimulus "
    "to earn the application point. Vague restatements of the stimulus without a "
    "geographic concept do not earn application credit."
)

_CS_A_GRADING = (
    "Accept functionally equivalent Java with minor syntax issues (missing "
    "semicolons, off-by-one variable names) unless the rubric explicitly requires "
    "strict syntax. Variable-name differences are not penalized. Award points for "
    "correct algorithmic intent even if Java idiom is non-canonical."
)

_PHYSICS_C_MECH_GRADING = (
    "Calculus-based mechanics. Award points per the rubric's structure (e.g. correct "
    "relationship/setup, substitution, final answer). Apply follow-through: an "
    "incorrect earlier value used correctly downstream still earns the later points. "
    "Accept algebraically and calculus-equivalent expressions and correct symbolic "
    "answers. Require correct units and vector direction where the rubric specifies. "
    "Do not award an answer point for a bare numerical result when the rubric requires "
    "supporting work."
)

_ENVIRONMENTAL_SCIENCE_GRADING = (
    "FRQs are point-based and reward specificity. Vague or generic statements earn no "
    "credit — require a concrete mechanism, named example, or specific cause-and-effect "
    "link. For calculation parts, require the setup/equation and correct units, and "
    "apply follow-through on arithmetic. 'Describe/Explain' demands detail; 'Identify' "
    "may be brief."
)

_WORLD_HISTORY_GRADING = (
    "Apply the College Board DBQ/LEQ rubric structure: Thesis/Claim (a defensible "
    "claim that responds to the prompt), Contextualization (broader historical setting), "
    "Evidence (specific and relevant; for DBQs, use of and sourcing of documents via "
    "HIPP), and Analysis & Reasoning (historical reasoning plus complexity). Award each "
    "rubric point independently when its specific criterion is met — do not require a "
    "flawless essay."
)

_PHYSICS_C_EM_GRADING = (
    "Calculus-based E&M. Award points per the rubric's structure (correct "
    "relationship/setup, substitution, final answer). Apply follow-through: an "
    "incorrect earlier value used correctly downstream still earns the later points. "
    "Accept algebraically and calculus-equivalent expressions (including correct use "
    "of integrals/derivatives, Gauss's/Ampère's law, etc.) and correct symbolic "
    "answers. Require correct units and direction where the rubric specifies."
)

_ENGLISH_LANGUAGE_GRADING = (
    "Apply the 6-point analytic rubric: Thesis (1 pt — a defensible position responding "
    "to the prompt), Evidence & Commentary (up to 4 pts — specific evidence plus a clear "
    "line of reasoning), and Sophistication (1 pt — genuine nuance/complexity, awarded "
    "sparingly). Reward a defensible thesis and specific support; do not penalize minor "
    "grammar or spelling unless it obscures meaning."
)

_COMPARATIVE_GOV_GRADING = (
    "Require use of specific course concepts and, where the prompt calls for it, accurate "
    "examples from the six core countries (UK, Russia, China, Mexico, Iran, Nigeria). For "
    "each point require BOTH a correct concept/definition AND its application to the "
    "scenario or country. Generic statements without a course concept do not earn "
    "application credit."
)

_BIOLOGY_OCR = (
    "For biological diagrams (cell, organelle, tissue, organ, organism): "
    "name every structure the student labelled and where it sits relative "
    "to others (nucleus inside cytoplasm, mitochondrion adjacent to ribosome, "
    "etc.). For cycle diagrams (Krebs, Calvin, cell cycle, nitrogen cycle): "
    "name each stage in order, the direction of every arrow, and any "
    "inputs/outputs the student wrote on the arrows. For experimental graphs: "
    "axes (label + units + scale), trend per group/treatment, error bars or "
    "ranges if drawn, and any annotation the student added (asterisks for "
    "significance, labelled controls vs experimentals). For pedigrees, "
    "Punnett squares, gel images: row/column layout and what's in each cell."
)

_CHEMISTRY_OCR = (
    "Diagram fidelity matters more than for other subjects — rubric points "
    "are awarded for specific bonds, lone pairs, charges and geometries the "
    "student drew. For Lewis / dot structures: name every atom by element "
    "symbol, every bond by its multiplicity and the two atoms it joins "
    "(e.g. 'C=O', 'N-H'), every lone pair (count and on which atom), every "
    "formal charge with the sign and adjacent atom, and the overall shape "
    "(bent, trigonal planar, tetrahedral, octahedral, etc.) when discernible. "
    "For intermolecular-force diagrams (hydrogen bonds, dipole-dipole, etc.): "
    "for each interaction line drawn, state the donor atom (and which H on it), "
    "the acceptor atom (and which lone pair on it), the molecule each belongs "
    "to, and the position on the page (left/right/top/bottom of the central "
    "species). For PES/spectroscopy: count peaks, give relative heights and "
    "x-position (binding energy or wavelength) for each, and identify exactly "
    "which peaks the student circled/marked using both absolute position "
    "(numeric x value or rough range) and relative language ('rightmost two', "
    "'leftmost', 'tallest'). For reaction-energy / potential-energy diagrams: "
    "describe each peak's relative height versus the others, the position and "
    "label of any intermediate, and whether reactants are higher or lower than "
    "products. For volumetric / glassware sketches: say whether the meniscus "
    "is concave or convex, where its bottom sits relative to the calibration "
    "line, and what (if anything) is labelled."
)

_PHYSICS_C_MECH_OCR = (
    "For free-body / force diagrams: every vector with its tail point, head "
    "direction (up/down/left/right or angle), labelled magnitude (mg, N, T, "
    "f, etc.), and what object it acts on. For motion / kinematics sketches: "
    "coordinate axes, position vs time / velocity vs time / acceleration vs "
    "time curves with their shape (linear, parabolic, constant) and key "
    "values at labelled points. For graphs generally: full axis labels with "
    "units, scale, every plotted point or curve, area under curve if shaded."
)

_PHYSICS_C_EM_OCR = (
    "For circuit diagrams: each component (resistor, capacitor, battery, "
    "switch, inductor) with its labelled value, connection topology, and "
    "labelled current direction or polarity. For field diagrams: arrow "
    "directions (electric / magnetic), arrow density relative to source, "
    "labelled magnitudes, and any Gaussian/Amperian surfaces drawn. For "
    "graphs: axes, scale, every plotted feature including discontinuities."
)


# ---------------------------------------------------------------------------
# course_configs.id -> addenda
# ---------------------------------------------------------------------------

GRADING_ADDENDA: dict[int, str] = {
    14: _BIOLOGY_GRADING,                  # AP Biology
    15: _STATISTICS_GRADING,               # AP Statistics
    16: _CHEMISTRY_GRADING,                # AP Chemistry
    18: _PRECALCULUS_GRADING,              # AP Pre-Calculus
    20: _HISTORY_DBQ_LEQ,                  # AP European History (authored)
    23: _PHYSICS1_GRADING,                 # AP Physics 1: Algebra (authored)
    24: _PHYSICS2_GRADING,                 # AP Physics 2: Algebra (authored)
    25: _HUMAN_GEOGRAPHY_GRADING,          # AP Human Geography
    26: _CS_A_GRADING,                     # AP Computer Science-A
    27: _HISTORY_DBQ_LEQ,                  # AP United States History (authored)
    28: _PHYSICS_C_MECH_GRADING,           # AP Physics C: Mechanics
    29: _ENVIRONMENTAL_SCIENCE_GRADING,    # AP Environmental Science
    30: _WORLD_HISTORY_GRADING,            # AP World History (Modern)
    32: _PHYSICS_C_EM_GRADING,             # AP Physics C: Electricity & Magnetism
    33: _ENGLISH_LANGUAGE_GRADING,         # AP English Language and Composition
    34: _ENGLISH_LITERATURE_GRADING,       # AP English Literature and Composition (authored)
    35: _COMPARATIVE_GOV_GRADING,          # AP Comparative Government and Politics
    36: _US_GOVERNMENT_GRADING,            # AP United States Government and Politics (authored)
}

OCR_ADDENDA: dict[int, str] = {
    14: _BIOLOGY_OCR,                      # AP Biology
    16: _CHEMISTRY_OCR,                    # AP Chemistry
    23: _PHYSICS1_OCR,                     # AP Physics 1 (authored)
    24: _PHYSICS2_OCR,                     # AP Physics 2 (authored)
    28: _PHYSICS_C_MECH_OCR,               # AP Physics C: Mechanics
    29: _ENVIRONMENTAL_SCIENCE_OCR,        # AP Environmental Science (authored)
    32: _PHYSICS_C_EM_OCR,                 # AP Physics C: Electricity & Magnetism
}


# course_configs.id -> course_name (verbatim from the supplied CSV). These rows
# are INSERTed because the catalog was missing them.
COURSES: dict[int, str] = {
    14: "AP Biology",
    15: "AP Statistics",
    16: "AP Chemistry",
    18: "AP Pre-Calculus",
    20: "AP European History",
    23: "AP Physics 1: Algebra",
    24: "AP Physics 2: Algebra",
    25: "AP Human Geography",
    26: "AP Computer Science-A",
    27: "AP United States History",
    28: "AP Physics C : Mechanics",
    29: "AP Environmental Science",
    30: "AP World History (Modern)",
    32: "AP Physics C: Electricity & Magnetism",
    33: "AP English Language and Composition",
    34: "AP English Literature and Composition",
    35: "AP Comparative Government and Politics",
    36: "AP United States Government and Politics",
}

# Placeholders for required NOT-NULL columns the grader doesn't use; they mirror
# the existing SAT row so the constraints pass and the grader can read the row.
_EXAM_BODY = "College Board"
_CATEGORY = "prep"            # placeholder (matches the SAT row)
_SCORING_TYPE = "composite"   # placeholder (matches the SAT row)

_slug_re = re.compile(r"[^a-z0-9]+")


def _subject_slug(course_name: str) -> str:
    """Lowercase hyphen slug of the course name (drops a leading 'AP ')."""
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
