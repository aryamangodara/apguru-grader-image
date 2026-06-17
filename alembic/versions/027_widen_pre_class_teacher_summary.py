"""Widen class.pre_class_teacher_summary to TEXT.

The pre-class teacher brief is now multi-section markdown (~1-2 KB) and overflows
the legacy ``VARCHAR(500)`` column, which silently truncated briefs mid-section.
This brings the column's width under version control instead of relying on an
out-of-band manual ALTER per environment.

DELIBERATE, NARROW EXCEPTION to the "``class`` is PHP-managed / not
Alembic-tracked" convention: this migration widens ONLY this one column via raw,
backticked DDL (MySQL-only, matching migration 026's style) and touches nothing
else on the table. It must never be regenerated via ``--autogenerate`` — the
rest of ``class`` is owned by the PHP app and is intentionally absent from
``Base.metadata``.

Revision ID: 027
Revises: 026
Create Date: 2026-06-17
"""

from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Store the markdown brief intact. Re-applying TEXT when already TEXT is a
    # harmless rebuild, so this is safe even if a column was widened by hand
    # out of band in some environment.
    op.execute(
        "ALTER TABLE `class` MODIFY `pre_class_teacher_summary` TEXT NULL"
    )


def downgrade() -> None:
    # Revert to the legacy width. Under MySQL strict mode (the default), the
    # narrowing ALTER aborts with "Data too long" if any brief exceeds 500
    # chars, so pre-truncate first. This is lossy: briefs over 500 chars are cut.
    op.execute(
        "UPDATE `class` SET `pre_class_teacher_summary` = "
        "LEFT(`pre_class_teacher_summary`, 500) "
        "WHERE CHAR_LENGTH(`pre_class_teacher_summary`) > 500"
    )
    op.execute(
        "ALTER TABLE `class` MODIFY `pre_class_teacher_summary` VARCHAR(500) NULL"
    )
