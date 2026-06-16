"""Add quiz_types catalog + config table.

Replaces the ``AI_QUIZ_TYPE = 3`` style int sentinel with a real
catalog whose rows are the v1 **quiz intents** — the set of
pedagogical purposes a quiz can have. Each row supplies catalog
defaults (num_questions range, timing, hints, explanation policy,
default Pinecone filter, fallback curation prompt key); the
Stage-1 LLM is free to override per-blueprint values within the
allowed bounds.

Pool source (A/B/C/D) and quiz intent are *independent axes*:
intent describes what the student is doing; pool describes where
the candidate topic came from. A single quiz can mix members from
multiple pools when pedagogy supports it. Intent does NOT pin pool.

Seeded with the v1 intent set:

- ``homework_review``  — quick verification on recently-taught material
                         (typically draws Pool B; multi-subtopic)
- ``sr_revision``      — spaced-repetition retrieval on due/overdue items
                         (typically Pool A; mixed subtopics)
- ``targeted_drill``   — confidence-rebuild on a weak subtopic family
                         (typically Pool D; narrow scope, hints on)
- ``diagnostic_intro`` — gentle exposure to an unseen subtopic
                         (typically Pool C; easy difficulty)
- ``mistake_review``   — replay of skills failed on a recent test;
                         dispatches to ``failed_questions_by_test``
                         strategy unchanged. No Stage-2 LLM curation.

Future intents (mock-style timed, speed drill, synthesis) land as
INSERTs without code changes — Open/Closed.

Revision ID: 011
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quiz_types",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "name",
            sa.String(64),
            nullable=False,
            comment="Stable machine name, e.g. 'recall_sprint'. "
            "Mirrored as quiz_subtype on weekly_plan_tasks for log/debug clarity.",
        ),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column(
            "strategy_name",
            sa.String(64),
            nullable=False,
            comment="Registry key into app/services/quiz_strategies/ — "
            "the curator/source strategy used at materialization.",
        ),
        sa.Column(
            "default_num_questions_min",
            sa.Integer,
            nullable=False,
            server_default=sa.text("5"),
        ),
        sa.Column(
            "default_num_questions_max",
            sa.Integer,
            nullable=False,
            server_default=sa.text("10"),
        ),
        sa.Column(
            "default_is_timed",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "default_hints_enabled",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "default_explanations",
            sa.String(32),
            nullable=False,
            server_default="after_quiz",
            comment="after_quiz | after_each | never.",
        ),
        sa.Column(
            "default_difficulty_band",
            sa.String(32),
            nullable=False,
            server_default="mixed",
            comment="mixed | easy | easy_to_medium | low_mid | hard.",
        ),
        sa.Column(
            "default_pinecone_filter_json",
            sa.JSON,
            nullable=True,
            comment="Per-subtype Pinecone metadata-filter template. "
            "Materialization merges snapshotted values "
            "(subtopic_id, course_id, difficulty_band) into this template.",
        ),
        sa.Column(
            "curation_prompt_key",
            sa.String(64),
            nullable=True,
            comment="Key into app/prompts/quiz_curation_prompt.py. "
            "NULL when the subtype skips Gemini curation (e.g., mistake_review).",
        ),
        sa.Column(
            "is_active",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP,
            nullable=False,
            server_default=sa.text(
                "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
            ),
        ),
    )
    op.create_index(
        "uq_quiz_types_name",
        "quiz_types",
        ["name"],
        unique=True,
    )

    # ------------------------------------------------------------------
    # Seed v1 catalog. SQL kept inline (small fixed set, single migration).
    # ------------------------------------------------------------------
    op.execute(
        """
        INSERT INTO quiz_types (
            name, display_name, description, strategy_name,
            default_num_questions_min, default_num_questions_max,
            default_is_timed, default_hints_enabled,
            default_explanations, default_difficulty_band,
            default_pinecone_filter_json, curation_prompt_key
        ) VALUES
        (
            'homework_review',
            'Homework review',
            'Quick verification on subtopics taught in class within the last week. Often spans 2-4 subtopics from Pool B; can mix in a related D-pool weakness when pedagogically warranted.',
            'generic_curation',
            6, 12,
            0, 0,
            'after_quiz', 'easy_to_medium',
            JSON_OBJECT('is_official', 'FALSE'),
            'generic_curation'
        ),
        (
            'sr_revision',
            'Spaced-repetition revision',
            'Retrieval practice on due/overdue spaced-repetition items. Mixed difficulty; usually multiple subtopics; the student is expected to context-switch.',
            'generic_curation',
            8, 15,
            1, 0,
            'after_quiz', 'mixed',
            JSON_OBJECT('is_official', 'FALSE'),
            'generic_curation'
        ),
        (
            'targeted_drill',
            'Targeted drill',
            'Confidence-rebuild on a weak subtopic family. Narrow scope (1-2 closely-related subtopics from Pool D), hints enabled, untimed, lean toward easier end of band.',
            'generic_curation',
            10, 15,
            0, 1,
            'after_quiz', 'low_mid',
            JSON_OBJECT('is_official', 'FALSE'),
            'generic_curation'
        ),
        (
            'diagnostic_intro',
            'Diagnostic intro',
            'Gentle exposure to an unseen subtopic (Pool C). Easy questions with explanations after every item; not assessed.',
            'generic_curation',
            5, 8,
            0, 0,
            'after_each', 'easy',
            JSON_OBJECT('is_official', 'FALSE'),
            'generic_curation'
        ),
        (
            'mistake_review',
            'Mistake review',
            'Replays the skills the student failed on their most recent test. Uses the existing failed_questions_by_test strategy seeded with a snapshotted test_id; no Stage-2 LLM curation.',
            'failed_questions_by_test',
            5, 20,
            0, 0,
            'after_quiz', 'mixed',
            NULL,
            NULL
        )
        """
    )


def downgrade() -> None:
    op.drop_index("uq_quiz_types_name", table_name="quiz_types")
    op.drop_table("quiz_types")
