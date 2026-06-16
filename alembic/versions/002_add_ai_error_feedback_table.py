"""Add ai_error_feedback table for caching LLM-generated error feedback.

Revision ID: 002
Create Date: 2026-04-03
"""

from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_error_feedback",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("question_id", sa.BigInteger, nullable=False),
        sa.Column("option_id", sa.BigInteger, nullable=False),
        sa.Column("option_label", sa.String(5), nullable=False),
        sa.Column("option_text", sa.Text, nullable=True),
        sa.Column("why_wrong", sa.Text, nullable=True),
        sa.Column("improvement_tip", sa.Text, nullable=True),
        sa.Column("common_misconception", sa.Text, nullable=True),
        sa.Column(
            "status",
            sa.Enum("generating", "completed", "failed",
                    name="ai_error_feedback_status"),
            nullable=False,
            server_default="generating",
        ),
        sa.Column("llm_model", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "uq_question_option",
        "ai_error_feedback",
        ["question_id", "option_id"],
        unique=True,
    )
    op.create_index(
        "idx_aef_question_id",
        "ai_error_feedback",
        ["question_id"],
    )
    op.create_index(
        "idx_aef_status",
        "ai_error_feedback",
        ["status"],
    )


def downgrade() -> None:
    op.drop_table("ai_error_feedback")
