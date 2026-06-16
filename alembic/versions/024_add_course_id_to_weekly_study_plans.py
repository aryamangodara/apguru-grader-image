"""Add course_id to weekly_study_plans (multi-course plan identity).

A student can be enrolled in multiple courses (SAT + AP, etc.). Plans were
keyed UNIQUE(student_id, week_start_date), so a second course's plan
overwrote the first. This adds course_id to the plan header and re-keys
uniqueness to (student_id, course_id, week_start_date).

Backfill: every pre-existing plan was produced by the SAT-only planner, so
course_id is set from the student's first active student_course_mapping row
(mirrors WeeklyPlanRepository.get_student_course: ORDER BY id ASC LIMIT 1),
COALESCE-ing to 1 (SAT) for any plan whose student has no live mapping.

Revision ID: 024
Create Date: 2026-06-11
"""

from alembic import op
import sqlalchemy as sa


revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "weekly_study_plans",
        sa.Column(
            "course_id",
            sa.BigInteger,
            nullable=True,
            comment="Course this plan belongs to. Part of the plan's "
            "identity: UNIQUE(student_id, course_id, week_start_date).",
        ),
    )
    # Backfill from the student's first active mapping (mirrors
    # get_student_course: ORDER BY id ASC LIMIT 1), fallback SAT (1).
    op.execute(
        """
        UPDATE weekly_study_plans p
        SET p.course_id = COALESCE(
            (SELECT scm.course_id
               FROM student_course_mapping scm
              WHERE scm.student_id = p.student_id
                AND scm.deleted_at IS NULL
              ORDER BY scm.id ASC
              LIMIT 1),
            1
        )
        WHERE p.course_id IS NULL
        """
    )
    op.alter_column(
        "weekly_study_plans",
        "course_id",
        existing_type=sa.BigInteger,
        nullable=False,
    )
    op.drop_index("uq_wsp_student_week", table_name="weekly_study_plans")
    op.create_index(
        "uq_wsp_student_course_week",
        "weekly_study_plans",
        ["student_id", "course_id", "week_start_date"],
        unique=True,
    )


def downgrade() -> None:
    # NOTE: if multi-course data exists (>1 plan per student+week), recreating
    # the old unique key will fail — that's expected; downgrade is only safe
    # before multi-course plans are written.
    op.drop_index("uq_wsp_student_course_week", table_name="weekly_study_plans")
    op.create_index(
        "uq_wsp_student_week",
        "weekly_study_plans",
        ["student_id", "week_start_date"],
        unique=True,
    )
    op.drop_column("weekly_study_plans", "course_id")
