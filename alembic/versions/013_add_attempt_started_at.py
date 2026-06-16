"""Add attempt_started_at column to student_todo_quiz_mapping.

Supports the server-authoritative 15-minute timer for AI custom quizzes.
``GET /quiz/{id}/play`` lazily stamps this column on first load and
``POST /quiz/{id}/submit`` reads it to enforce the 15:00 + 1:00 grace
window. NULL means the student has not yet started the attempt.

Revision ID: 013
Create Date: 2026-05-01

NOTE: this revision was originally numbered 007 but collided with the
existing ``007_add_subtopics_table.py``. The current chain head when
this migration was authored is ``012`` (polymorphic_weekly_plan_tasks),
so this is now the next sequential number.
"""

from alembic import op
import sqlalchemy as sa

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "student_todo_quiz_mapping",
        sa.Column("attempt_started_at", sa.DateTime(), nullable=True),
    )
    # Index it so seconds_remaining computations on the Practice list stay fast.
    op.create_index(
        "ix_student_todo_quiz_mapping_attempt_started_at",
        "student_todo_quiz_mapping",
        ["attempt_started_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_student_todo_quiz_mapping_attempt_started_at",
        table_name="student_todo_quiz_mapping",
    )
    op.drop_column("student_todo_quiz_mapping", "attempt_started_at")
