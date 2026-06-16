"""Add weekly plan tables.

Introduces the three tables backing the weekly-plan feature:

- ``weekly_study_plans``: one row per (student_id, week_start_date)
  with the plan header (stage, budget snapshot, pool quotas,
  must-include snapshot, source model, goal summary). Replaced on
  regeneration; ``UNIQUE (student_id, week_start_date)`` enables
  ``INSERT ... ON DUPLICATE KEY UPDATE`` for the replace-outright
  semantics agreed on for v1.

- ``weekly_plan_tasks``: line items under a plan. Each task is
  anchored to a (topic, subtopic) and tagged with its source pool
  (A/B/C/D) + ``is_timed`` flag for Pool D. Task-level status
  tracking lives here so per-day completion state survives an
  ad-hoc regen via status carry-over on matching (topic, subtopic)
  pairs (carry-over logic lives in app code, not the schema).
  ``topic_name`` and ``subtopic_name`` are denormalized snapshots
  so renamed / soft-deleted references don't break the UI.

- ``student_plan_preferences``: one row per student. Currently only
  ``minutes_by_day_json`` (a JSON object keyed by weekday abbrev,
  e.g. ``{"mon": 60, "tue": 60, ...}``). Stored as JSON from day
  one so supporting per-day variation doesn't require a later
  schema change.

All FKs are logical (no ``ForeignKeyConstraint``) — consistent with
``006_add_spaced_repetition_table.py`` and project convention.

Revision ID: 008
Create Date: 2026-04-24
"""

from alembic import op
import sqlalchemy as sa


revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # weekly_study_plans — plan header, one per (student, week_start)
    # ------------------------------------------------------------------
    op.create_table(
        "weekly_study_plans",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("week_start_date", sa.Date, nullable=False),
        sa.Column("weekly_goal_summary", sa.Text, nullable=True),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column(
            "minutes_by_day_snapshot_json",
            sa.JSON,
            nullable=False,
            comment="Snapshot of the per-day minute budget used at "
            "generation time. Shape: {weekday_abbrev: int}.",
        ),
        sa.Column(
            "pool_quotas_json",
            sa.JSON,
            nullable=True,
            comment="Snapshot of pool quotas {A: int, B: int, C: int, D: int} "
            "used at generation time (for audit).",
        ),
        sa.Column(
            "must_include_topic_ids_json",
            sa.JSON,
            nullable=True,
            comment="Snapshot of the must-include topic_ids list used "
            "at generation time (for audit).",
        ),
        # ------------------------------------------------------------------
        # LLM-tuned pool quota knobs (see PLAN-weekly-plan-emphasis-tuning.md).
        # All three are nullable so legacy / fallback writes don't fail.
        # ------------------------------------------------------------------
        sa.Column(
            "improvement_emphasis",
            sa.Numeric(3, 2),
            nullable=True,
            comment="LLM-decided scalar in [0.0, 1.0] used to nudge B<->D pool "
            "ratios from the stage baseline. 0.5 = no nudge.",
        ),
        sa.Column(
            "enable_pool_c",
            sa.Boolean,
            nullable=True,
            comment="LLM-decided boolean. False = skip Pool C entirely "
            "(redistribute its budget across A/B/D).",
        ),
        sa.Column(
            "quota_rationale",
            sa.Text,
            nullable=True,
            comment="LLM rationale for the emphasis + pool_c decision. "
            "References specific snapshot fields by name.",
        ),
        sa.Column("source_model", sa.String(100), nullable=True),
        sa.Column(
            "generated_by",
            sa.String(32),
            nullable=False,
            server_default="system",
            comment="system = scheduled nightly; ad_hoc = user-triggered.",
        ),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="ready",
            comment="pending | generating | ready | failed",
        ),
        sa.Column(
            "error_message",
            sa.Text,
            nullable=True,
            comment="Diagnostics for status='failed' rows.",
        ),
        sa.Column(
            "generated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
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
        "uq_wsp_student_week",
        "weekly_study_plans",
        ["student_id", "week_start_date"],
        unique=True,
    )
    op.create_index(
        "idx_wsp_student_generated",
        "weekly_study_plans",
        ["student_id", "generated_at"],
    )

    # ------------------------------------------------------------------
    # weekly_plan_tasks — ordered line items under a plan
    # ------------------------------------------------------------------
    op.create_table(
        "weekly_plan_tasks",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("plan_id", sa.BigInteger, nullable=False),
        sa.Column(
            "day_index",
            sa.SmallInteger,
            nullable=False,
            comment="0=Monday .. 6=Sunday.",
        ),
        sa.Column(
            "order_index",
            sa.SmallInteger,
            nullable=False,
            comment="Ordering within a day, starting at 0.",
        ),
        sa.Column("topic_id", sa.Integer, nullable=False),
        sa.Column("subtopic_id", sa.BigInteger, nullable=False),
        sa.Column("topic_name_snapshot", sa.String(255), nullable=False),
        sa.Column("subtopic_name_snapshot", sa.String(255), nullable=False),
        sa.Column(
            "pool",
            sa.String(1),
            nullable=False,
            comment="A = spaced repetition, B = recent class, "
            "C = unseen topics, D = mock-test weak.",
        ),
        sa.Column(
            "is_timed",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "difficulty",
            sa.String(16),
            nullable=False,
            comment="easy | medium | hard.",
        ),
        sa.Column("duration_minutes", sa.Integer, nullable=False),
        sa.Column("rationale", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.String(16),
            nullable=False,
            server_default="planned",
            comment="planned | completed | skipped.",
        ),
        sa.Column("completed_at", sa.DateTime, nullable=True),
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
        "idx_wpt_plan_day_order",
        "weekly_plan_tasks",
        ["plan_id", "day_index", "order_index"],
    )
    op.create_index(
        "idx_wpt_plan_status",
        "weekly_plan_tasks",
        ["plan_id", "status"],
    )

    # ------------------------------------------------------------------
    # student_plan_preferences — one row per student
    # ------------------------------------------------------------------
    op.create_table(
        "student_plan_preferences",
        sa.Column(
            "student_id",
            sa.BigInteger,
            primary_key=True,
            autoincrement=False,
        ),
        sa.Column(
            "minutes_by_day_json",
            sa.JSON,
            nullable=False,
            comment='Per-day minute budget, e.g. {"mon": 60, "tue": 60, ...}. '
            "Missing keys mean no study scheduled that day.",
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


def downgrade() -> None:
    op.drop_table("student_plan_preferences")
    op.drop_index(
        "idx_wpt_plan_status", table_name="weekly_plan_tasks"
    )
    op.drop_index(
        "idx_wpt_plan_day_order", table_name="weekly_plan_tasks"
    )
    op.drop_table("weekly_plan_tasks")
    op.drop_index(
        "idx_wsp_student_generated", table_name="weekly_study_plans"
    )
    op.drop_index(
        "uq_wsp_student_week", table_name="weekly_study_plans"
    )
    op.drop_table("weekly_study_plans")
