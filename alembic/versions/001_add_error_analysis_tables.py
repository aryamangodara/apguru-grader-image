"""Add error_analysis_runs and error_analysis_flags tables.

Revision ID: 001
Create Date: 2026-03-26
"""

from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "error_analysis_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("course_id", sa.Integer, nullable=False),
        sa.Column("section_id", sa.Integer, nullable=True),
        # Logical FK to the legacy ``online_test_setup`` table. Nullable
        # so cron-style "all answers" runs (no specific test) still work.
        # Application reads this in ``error_analysis_persistence._create_run``
        # and ``get_error_trends``; previously added via ad-hoc ALTER TABLE
        # against existing DBs and never back-filled into this migration.
        sa.Column("test_id", sa.BigInteger, nullable=True),
        sa.Column(
            "status",
            sa.Enum("pending", "heuristics_done", "completed", "failed",
                    name="run_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("total_questions", sa.Integer, nullable=True),
        sa.Column("llm_total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("llm_completed", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime, nullable=True),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "idx_runs_student_status",
        "error_analysis_runs",
        ["student_id", "status"],
    )
    op.create_index(
        "idx_runs_student_created",
        "error_analysis_runs",
        ["student_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "idx_runs_course_section",
        "error_analysis_runs",
        ["course_id", "section_id"],
    )

    op.create_table(
        "error_analysis_flags",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.BigInteger, nullable=False),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("attempt_id", sa.BigInteger, nullable=False),
        sa.Column("question_id", sa.BigInteger, nullable=False),
        sa.Column("course_id", sa.Integer, nullable=False),
        sa.Column("section_id", sa.Integer, nullable=True),
        sa.Column("error_type", sa.String(30), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("needs_llm", sa.Boolean, nullable=False, server_default="0"),
        sa.Column(
            "flag_status",
            sa.Enum("classified", "pending_llm", "llm_done",
                    name="flag_status"),
            nullable=False,
            server_default="classified",
        ),
        sa.Column("evidence", sa.JSON, nullable=False),
        sa.Column("llm_error_type", sa.String(30), nullable=True),
        sa.Column("llm_confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("llm_reasoning", sa.Text, nullable=True),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["error_analysis_runs.id"],
            ondelete="CASCADE",
        ),
        # ``attempt_id`` references the legacy ``online_student_test_answers``
        # table which is not alembic-managed. Its ``id`` column type
        # (BIGINT UNSIGNED on this MySQL instance) is incompatible with the
        # signed ``BIGINT`` declared above, so the FK is enforced *logically*
        # in application code instead — matches the convention documented in
        # ``006_add_spaced_repetition_table.py`` and ``008_add_weekly_plan_tables.py``.
    )
    op.create_index("idx_flags_run", "error_analysis_flags", ["run_id"])
    op.create_index("idx_flags_student", "error_analysis_flags", ["student_id"])
    op.create_index(
        "idx_flags_status",
        "error_analysis_flags",
        ["run_id", "flag_status"],
    )
    op.create_index(
        "idx_flags_error_type",
        "error_analysis_flags",
        ["error_type"],
    )


def downgrade() -> None:
    op.drop_table("error_analysis_flags")
    op.drop_table("error_analysis_runs")
