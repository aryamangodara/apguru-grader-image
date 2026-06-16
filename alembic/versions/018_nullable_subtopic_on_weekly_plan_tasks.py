"""Make subtopic_id and subtopic_name_snapshot nullable on weekly_plan_tasks.

International curriculums may not have subtopic-level classification.
The weekly-plan pipeline now generates plans for topic-only candidates,
producing members with ``subtopic_id = NULL``. The flat legacy columns
on ``weekly_plan_tasks`` must accept NULL to match.

Revision ID: 018
Create Date: 2026-05-13
"""

import sqlalchemy as sa
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "weekly_plan_tasks",
        "subtopic_id",
        nullable=True,
        existing_type=sa.BigInteger,
    )
    op.alter_column(
        "weekly_plan_tasks",
        "subtopic_name_snapshot",
        nullable=True,
        existing_type=sa.String(255),
    )


def downgrade() -> None:
    op.execute("UPDATE weekly_plan_tasks SET subtopic_id = 0 WHERE subtopic_id IS NULL")
    op.execute(
        "UPDATE weekly_plan_tasks SET subtopic_name_snapshot = '' "
        "WHERE subtopic_name_snapshot IS NULL"
    )
    op.alter_column(
        "weekly_plan_tasks",
        "subtopic_id",
        nullable=False,
        existing_type=sa.BigInteger,
    )
    op.alter_column(
        "weekly_plan_tasks",
        "subtopic_name_snapshot",
        nullable=False,
        existing_type=sa.String(255),
    )
