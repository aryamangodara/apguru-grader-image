"""Add quiz_pattern_analyses table.

Caches the per-quiz AI error-pattern analyzer output keyed by
(student_id, quiz_id). A row is valid iff:
    cached.prompt_version == PROMPT_VERSION
    AND cached.llm_model == current_llm_model
    AND cached.latest_attempt_at >= db.MAX(created_at) for the answers

Structurally identical to test_pattern_analyses (migration 014); only
the FK column differs (quiz_id instead of test_id). Caches the streamed
Markdown body (``patterns_markdown``), the parsed action plan
(``action_plan``, NULL when the LLM emitted an unparseable block), and
the question_meta payload so replay can reconstruct the original SSE
stream byte-for-byte.

Revision ID: 016
Create Date: 2026-05-12
"""

from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quiz_pattern_analyses",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("student_id", sa.BigInteger, nullable=False),
        sa.Column("quiz_id", sa.BigInteger, nullable=False),
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
        "uq_quiz_pattern_analyses_student_quiz",
        "quiz_pattern_analyses",
        ["student_id", "quiz_id"],
        unique=True,
    )
    op.create_index(
        "ix_quiz_pattern_analyses_student_recent",
        "quiz_pattern_analyses",
        ["student_id", "generated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quiz_pattern_analyses_student_recent",
        table_name="quiz_pattern_analyses",
    )
    op.drop_index(
        "uq_quiz_pattern_analyses_student_quiz",
        table_name="quiz_pattern_analyses",
    )
    op.drop_table("quiz_pattern_analyses")
