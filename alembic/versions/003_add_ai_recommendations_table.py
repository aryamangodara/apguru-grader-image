"""Add ai_recommendations table for caching LLM-generated study recommendations.

Revision ID: 003
Create Date: 2026-04-06
"""

from alembic import op
import sqlalchemy as sa

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_recommendations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("section", sa.String(20), nullable=False, server_default="all"),
        sa.Column(
            "status",
            sa.Enum("generating", "completed", "failed",
                    name="ai_recommendations_status"),
            nullable=False,
            server_default="generating",
        ),
        sa.Column("recommendations", sa.JSON, nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=True),
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
        "uq_student_section",
        "ai_recommendations",
        ["student_id", "section"],
        unique=True,
    )
    op.create_index(
        "idx_airec_student",
        "ai_recommendations",
        ["student_id"],
    )


def downgrade() -> None:
    op.drop_table("ai_recommendations")
