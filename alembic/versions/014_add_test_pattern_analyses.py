"""Add test_pattern_analyses table.

Caches the per-test AI error-pattern analyzer output keyed by
(student_id, test_id). A row is valid iff:
    cached.prompt_version == PROMPT_VERSION
    AND cached.llm_model == current_llm_model
    AND cached.latest_attempt_at >= db.MAX(created_at) for the answers

Stores both the streamed Markdown body (``patterns_markdown``, everything
BEFORE the JSON sentinel) and the parsed action plan JSON
(``action_plan``, NULL when the LLM emitted an unparseable block).
``question_meta`` mirrors the SSE ``event: questions`` payload so the
cache-replay path can fully reconstruct the original stream.

Revision ID: 014
Create Date: 2026-05-04
"""

from alembic import op
import sqlalchemy as sa

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "test_pattern_analyses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("test_id", sa.BigInteger, nullable=False),
        sa.Column("course_id", sa.BigInteger, nullable=True),
        sa.Column("llm_model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(20), nullable=False),
        sa.Column("patterns_markdown", sa.dialects.mysql.LONGTEXT(), nullable=False),
        sa.Column("action_plan", sa.JSON, nullable=True),
        sa.Column("question_meta", sa.JSON, nullable=False),
        sa.Column("generation_latency_ms", sa.Integer, nullable=False),
        sa.Column("latest_attempt_at", sa.DateTime, nullable=False),
        sa.Column(
            "generated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )
    op.create_index(
        "uq_test_pattern_analyses_student_test",
        "test_pattern_analyses",
        ["student_id", "test_id"],
        unique=True,
    )
    op.create_index(
        "ix_test_pattern_analyses_student_recent",
        "test_pattern_analyses",
        ["student_id", "generated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_test_pattern_analyses_student_recent",
        table_name="test_pattern_analyses",
    )
    op.drop_index(
        "uq_test_pattern_analyses_student_test",
        table_name="test_pattern_analyses",
    )
    op.drop_table("test_pattern_analyses")
