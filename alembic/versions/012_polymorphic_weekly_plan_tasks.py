"""Make ``weekly_plan_tasks`` polymorphic and add lazy-materialization fields.

A weekly plan is now a list of typed Tasks. Today only ``task_type =
'quiz'`` exists, but the schema is forward-compatible: future task
types (class, revision, material, ...) plug in without touching
existing rows.

For quiz tasks, the row also carries the **full snapshot** the
materialization service needs to lazily compose the actual quiz when
the student opens the task. Snapshotting at plan time means
materialization needs zero upstream lookups beyond the task row + the
Pinecone call — and shields students from mid-week drift (course
changes, topic renames, new tests taken).

Columns added to ``weekly_plan_tasks``:

- ``task_type``            - discriminator. Defaults to 'quiz'.
- ``quiz_type_id``         - FK (logical) to ``quiz_types(id)``.
                              NULL only for future non-quiz task types.
- ``task_payload_json``    - per-type snapshot block (subtype, names,
                              course/section snapshots, skill/domain,
                              effective config, subtype_extras).
- ``task_identity_key``    - replaces ``(topic_id, subtopic_id)`` as
                              the carry-over key on plan regen.
                              Generalizes to non-quiz task types.
- ``materialized_quiz_id`` - set on first task open; subsequent opens
                              are idempotent.
- ``materialized_at``      - timestamp of first materialization.

Existing flat columns (``topic_id``, ``subtopic_id``, ``pool``,
``is_timed``, ``difficulty``, name snapshots) are KEPT and **dual-
written** for one release so analytics queries don't break. A
follow-up migration drops them once code reads only from JSON.

Logical-FK convention (consistent with 006/008): the new FK columns
are plain ``BigInteger`` with no ``ForeignKeyConstraint``.

Revision ID: 012
Create Date: 2026-04-30
"""

from alembic import op
import sqlalchemy as sa


revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Add new columns. ``task_identity_key`` is added nullable first
    #    so the backfill can populate it; we'll tighten to NOT NULL at
    #    the end of the upgrade.
    # ------------------------------------------------------------------
    op.add_column(
        "weekly_plan_tasks",
        sa.Column(
            "task_type",
            sa.String(32),
            nullable=False,
            server_default="quiz",
            comment="Discriminator: quiz | (future: class | revision | material).",
        ),
    )
    op.add_column(
        "weekly_plan_tasks",
        sa.Column(
            "quiz_type_id",
            sa.BigInteger,
            nullable=True,
            comment="Logical FK to quiz_types.id. NULL only for future "
            "non-quiz task_types.",
        ),
    )
    op.add_column(
        "weekly_plan_tasks",
        sa.Column(
            "task_payload_json",
            sa.JSON,
            nullable=True,
            comment="Per-type snapshot block. For quiz: subtype, name "
            "snapshots, course/section/skill/domain snapshots, "
            "effective_quiz_config, subtype_extras.",
        ),
    )
    op.add_column(
        "weekly_plan_tasks",
        sa.Column(
            "task_identity_key",
            sa.String(255),
            nullable=True,  # tightened to NOT NULL after backfill below
            comment="Stable identity for status carry-over on plan "
            "regen. Position-based: quiz:{intent}:d{day_index}:o{order_index}. "
            "Survives mid-week regen even if member subtopics shift, "
            "because the slot's role in the week is what the student "
            "experienced as completed.",
        ),
    )
    op.add_column(
        "weekly_plan_tasks",
        sa.Column(
            "materialized_quiz_id",
            sa.BigInteger,
            nullable=True,
            comment="Logical FK to quiz.id. Set on first task open; "
            "idempotent on subsequent opens. NULL until materialized.",
        ),
    )
    op.add_column(
        "weekly_plan_tasks",
        sa.Column(
            "materialized_at",
            sa.DateTime,
            nullable=True,
        ),
    )

    # ------------------------------------------------------------------
    # 2. Backfill existing rows. Each legacy single-subtopic task is
    #    converted to a v1 quiz task with a single member. Pool -> intent
    #    is a best-effort map for legacy data; new tasks emitted by the
    #    Stage-1 LLM choose intent independently.
    #
    #      A -> sr_revision        (SR-driven retrieval)
    #      B -> homework_review    (recent class verification)
    #      C -> diagnostic_intro   (unseen exposure)
    #      D -> targeted_drill     (weak-topic drill)
    #
    #    task_identity_key uses the new position-based format so legacy
    #    rows participate in carry-over consistently with new ones.
    # ------------------------------------------------------------------
    op.execute(
        """
        UPDATE weekly_plan_tasks t
        SET t.quiz_type_id = (
            SELECT q.id FROM quiz_types q
            WHERE q.name = CASE t.pool
                WHEN 'A' THEN 'sr_revision'
                WHEN 'B' THEN 'homework_review'
                WHEN 'C' THEN 'diagnostic_intro'
                WHEN 'D' THEN 'targeted_drill'
                ELSE NULL
            END
        )
        WHERE t.quiz_type_id IS NULL
        """
    )
    # NOTE: backslash-escaped colons (``\\:``) keep SQLAlchemy's
    # ``text()`` from interpreting ``:d`` / ``:o`` as bind parameters.
    # In MySQL the backslash is consumed as a string escape, so the
    # column ends up holding literal ``quiz:{intent}:dN:oN``.
    op.execute(
        r"""
        UPDATE weekly_plan_tasks t
        SET t.task_identity_key = CONCAT(
            'quiz\:',
            COALESCE(
                (SELECT q.name FROM quiz_types q WHERE q.id = t.quiz_type_id),
                'legacy'
            ),
            '\:d', t.day_index, '\:o', t.order_index
        )
        WHERE t.task_identity_key IS NULL
        """
    )
    op.execute(
        """
        UPDATE weekly_plan_tasks t
        SET task_payload_json = JSON_OBJECT(
            'intent', (
                SELECT q.name FROM quiz_types q WHERE q.id = t.quiz_type_id
            ),
            'quiz_type_id', t.quiz_type_id,
            'course_id_snapshot', 0,
            'section_id_snapshot', 0,
            'members', JSON_ARRAY(
                JSON_OBJECT(
                    'topic_id', t.topic_id,
                    'subtopic_id', t.subtopic_id,
                    'topic_name', t.topic_name_snapshot,
                    'subtopic_name', t.subtopic_name_snapshot,
                    'pool', t.pool,
                    'skill', NULL,
                    'domain', NULL
                )
            ),
            'effective_quiz_config', JSON_OBJECT(
                'num_questions', 5,
                'duration_minutes', t.duration_minutes,
                'is_timed', t.is_timed = 1,
                'hints_enabled', FALSE,
                'explanations', 'after_quiz',
                'difficulty_band', t.difficulty
            ),
            'curation_hint', '',
            'subtype_extras', NULL,
            'materialized_quiz_id', NULL,
            'materialized_at', NULL
        )
        WHERE t.task_payload_json IS NULL
        """
    )

    # ------------------------------------------------------------------
    # 3. Tighten task_identity_key to NOT NULL (backfill is complete).
    # ------------------------------------------------------------------
    op.alter_column(
        "weekly_plan_tasks",
        "task_identity_key",
        existing_type=sa.String(255),
        nullable=False,
    )

    # ------------------------------------------------------------------
    # 4. Indexes — speed the carry-over WHERE clause and the
    #    plan-by-task-type analytics path.
    # ------------------------------------------------------------------
    op.create_index(
        "idx_wpt_plan_type",
        "weekly_plan_tasks",
        ["plan_id", "task_type"],
    )
    op.create_index(
        "idx_wpt_plan_identity",
        "weekly_plan_tasks",
        ["plan_id", "task_identity_key"],
    )


def downgrade() -> None:
    # The index drops are wrapped in best-effort try/except so the
    # downgrade is safe to run even if the upgrade failed mid-flight
    # (before CREATE INDEX). MySQL doesn't support
    # ``DROP INDEX IF EXISTS``, so we catch ``ProgrammingError`` for
    # the "index doesn't exist" case (1091) and continue.
    from sqlalchemy.exc import OperationalError, ProgrammingError

    for index_name in ("idx_wpt_plan_identity", "idx_wpt_plan_type"):
        try:
            op.drop_index(index_name, table_name="weekly_plan_tasks")
        except (ProgrammingError, OperationalError):
            # Index didn't exist (partial upgrade) — ignore and proceed.
            pass

    op.drop_column("weekly_plan_tasks", "materialized_at")
    op.drop_column("weekly_plan_tasks", "materialized_quiz_id")
    op.drop_column("weekly_plan_tasks", "task_identity_key")
    op.drop_column("weekly_plan_tasks", "task_payload_json")
    op.drop_column("weekly_plan_tasks", "quiz_type_id")
    op.drop_column("weekly_plan_tasks", "task_type")
