"""Add per-course ``time_bands`` to course_configs (full-review widget).

The Test Report "Time by Topic" widget rates each section/topic by its mean
seconds-per-question against a set of band cutoffs. Those cutoffs were a single
hardcoded global dict in ``app.core.constants`` — wrong, because a "normal" time
per question differs sharply by course (SAT vs. an AP subject vs. ACT). This
moves the cutoffs into ``course_configs`` so each course carries its own bands,
resolved per request and tunable without a deploy.

Shape: a JSON object of ``label -> inclusive max seconds-per-question``, walked
in declared order; the final label uses ``null`` as the open-ended catch-all::

    {"great": 30, "good": 60, "avg": 90, "bad": 120, "poor": null}

NULL column == course not configured. The service treats NULL as "no bands":
the widget then shows raw seconds with no rating label (see
``app.core.course_config.get_time_bands``).

Course 1 (SAT) is seeded here with the exact values that were previously the
global default, so SAT behavior is unchanged the moment this migration lands.
Other courses stay NULL until product tunes them.

Revision ID: 025
Create Date: 2026-06-12
"""

import json

from sqlalchemy import text

from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

# Previous global default, now seeded as SAT's per-course bands.
_SAT_TIME_BANDS = {
    "great": 30,
    "good": 60,
    "avg": 90,
    "bad": 120,
    "poor": None,
}


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `course_configs` "
        "ADD COLUMN `time_bands` JSON NULL "
        "COMMENT 'Full-review Time-by-Topic band cutoffs: "
        "{label: inclusive max seconds/question}, last label null = catch-all. "
        "NULL = no rating (widget shows raw seconds).'"
    )

    # Seed SAT (course 1) with the prior global defaults so behavior is
    # preserved on landing. Parameterized to keep the JSON literal out of SQL.
    op.get_bind().execute(
        text(
            "UPDATE `course_configs` SET `time_bands` = :bands "
            "WHERE `course_id` = 1 AND `is_active` = 1"
        ),
        {"bands": json.dumps(_SAT_TIME_BANDS)},
    )


def downgrade() -> None:
    op.execute("ALTER TABLE `course_configs` DROP COLUMN `time_bands`")
