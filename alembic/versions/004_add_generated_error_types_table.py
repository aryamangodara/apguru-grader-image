"""Add generated_error_types table for persisting error types.

Stores both LLM-generated and manually created error types,
linked to a course.

Revision ID: 004
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generated_error_types",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("course_id", sa.BigInteger, nullable=True),
        sa.Column("curriculum_name", sa.String(255), nullable=False),
        sa.Column("subject_area", sa.String(255), nullable=True),
        sa.Column("error_type_key", sa.String(100), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("fix", sa.Text, nullable=False),
        sa.Column("detection_criteria", sa.Text, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column(
            "source",
            sa.Enum("llm", "manual", name="generated_error_types_source"),
            nullable=False,
            server_default="llm",
        ),
        sa.Column(
            "status",
            sa.Enum("accepted", "rejected", name="generated_error_types_status"),
            nullable=False,
            server_default="accepted",
        ),
        sa.Column("created_by", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "idx_get_curriculum_name",
        "generated_error_types",
        ["curriculum_name"],
    )
    op.create_index(
        "idx_get_course_id",
        "generated_error_types",
        ["course_id"],
    )


def downgrade() -> None:
    op.drop_table("generated_error_types")
