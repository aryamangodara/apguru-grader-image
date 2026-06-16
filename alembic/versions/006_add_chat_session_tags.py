"""Add chat_session_tags table for user-editable session labels.

Supports two-tier tagging:
  - Preset thematic tags (test_prep, math_drill, english_drill,
    doubt_clearing, concept_review)
  - Custom free-form tags entered by the student

Revision ID: 006
Create Date: 2026-04-27
"""

from alembic import op
import sqlalchemy as sa

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_session_tags",
        sa.Column("id", sa.BigInteger(), nullable=False, autoincrement=True),
        sa.Column("student_id", sa.BigInteger(), nullable=False),
        sa.Column("session_id", sa.String(50), nullable=False),
        # tag: the slug or free-form label (e.g. "test_prep", "my custom tag")
        sa.Column("tag", sa.String(64), nullable=False),
        # is_preset: True when the tag is one of the 5 thematic presets
        sa.Column("is_preset", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "tag", name="uq_session_tag"),
    )
    op.create_index(
        "ix_chat_session_tags_student_session",
        "chat_session_tags",
        ["student_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_session_tags_student_session", table_name="chat_session_tags")
    op.drop_table("chat_session_tags")
