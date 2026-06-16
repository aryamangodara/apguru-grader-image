"""Widen error_type columns in error_analysis_flags.

The generated_error_types table uses VARCHAR(100) for error_type_key.
Widen error_analysis_flags.error_type and llm_error_type from
VARCHAR(30) to VARCHAR(100) to accommodate dynamic error type keys.

Revision ID: 005
Create Date: 2026-04-10
"""

from alembic import op
import sqlalchemy as sa

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "error_analysis_flags",
        "error_type",
        existing_type=sa.String(30),
        type_=sa.String(100),
        existing_nullable=False,
    )
    op.alter_column(
        "error_analysis_flags",
        "llm_error_type",
        existing_type=sa.String(30),
        type_=sa.String(100),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "error_analysis_flags",
        "error_type",
        existing_type=sa.String(100),
        type_=sa.String(30),
        existing_nullable=False,
    )
    op.alter_column(
        "error_analysis_flags",
        "llm_error_type",
        existing_type=sa.String(100),
        type_=sa.String(30),
        existing_nullable=True,
    )
