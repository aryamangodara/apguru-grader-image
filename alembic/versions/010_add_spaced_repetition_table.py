"""Add spaced_repetition table.

One row per (student_id, topic_id). Tracks SM-2 state used by the
spaced repetition service to schedule topic reviews.

See docs/spaced-repetition.md for the full algorithm spec.

Revision ID: 010
Create Date: 2026-04-16
"""

from alembic import op
import sqlalchemy as sa

revision = "010"
down_revision = "009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spaced_repetition",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("topic_id", sa.Integer, nullable=False),
        sa.Column("interval_days", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "ease_factor",
            sa.Numeric(4, 2),
            nullable=False,
            server_default="2.50",
        ),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("last_reviewed", sa.Date, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
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
        "uq_sr_student_topic",
        "spaced_repetition",
        ["student_id", "topic_id"],
        unique=True,
    )
    op.create_index(
        "idx_sr_student_due",
        "spaced_repetition",
        ["student_id", "due_date"],
    )


def downgrade() -> None:
    op.drop_index("idx_sr_student_due", table_name="spaced_repetition")
    op.drop_index("uq_sr_student_topic", table_name="spaced_repetition")
    op.drop_table("spaced_repetition")
