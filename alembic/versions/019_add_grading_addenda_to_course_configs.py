"""Add grading_addendum and ocr_addendum columns to course_configs.

The AP FRQ auto-grader resolves per-subject grading/OCR guidance dynamically
from ``course_configs`` at grade time (rather than hardcoding it in code), so
admins can edit guidance without a deploy. Seeding the actual text is a separate
data-only step — see ``scripts/seed_grader_addenda.py``.

Revision ID: 019
Create Date: 2026-06-02
"""

from alembic import op

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE `course_configs` "
        "ADD COLUMN `grading_addendum` TEXT NULL "
        "COMMENT 'AP FRQ grader: subject-specific grading guidance, injected into the grading prompt', "
        "ADD COLUMN `ocr_addendum` TEXT NULL "
        "COMMENT 'AP FRQ grader: subject-specific OCR/diagram guidance, injected into the OCR prompt'"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE `course_configs` "
        "DROP COLUMN `grading_addendum`, "
        "DROP COLUMN `ocr_addendum`"
    )
