"""Make course_configs.id a true AUTO_INCREMENT surrogate key.

In several environments ``course_configs.id`` ended up equal to ``course_id``
(SAT id=1/course_id=1, AP Biology id=14/course_id=14, ...) instead of being an
independent surrogate primary key. Two causes:

1. Migration ``015`` declares ``id int NOT NULL AUTO_INCREMENT`` but creates the
   table with ``CREATE TABLE IF NOT EXISTS``; where the table was created by hand
   before alembic (UAT/staging) that DDL was a no-op, so the live column may have
   no AUTO_INCREMENT attribute.
2. The seed migrations ``021``/``022`` insert an explicit ``id = course_id`` to
   mirror the legacy ``course.id`` convention.

This migration compacts the existing ids to a clean ``1..N`` sequence and forces
the column to ``AUTO_INCREMENT`` so future inserts get an independent id. Nothing
references ``course_configs.id`` (no foreign keys; all reads are by ``course_id``,
e.g. ``app/core/course_config.py``), so renumbering is safe.

``downgrade`` restores the old ``id == course_id`` mirror — reconstructable
because ``course_id`` is preserved and numeric for every current row.

Revision ID: 023
Create Date: 2026-06-05
"""

import sqlalchemy as sa

from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Compact existing ids to 1..N in ascending order. Because each new id is
    # always <= the row's old id (and new ids are dense from 1), no in-flight
    # primary-key collision is possible.
    rows = conn.execute(sa.text("SELECT id FROM course_configs ORDER BY id")).fetchall()
    for new_id, row in enumerate(rows, start=1):
        old_id = row[0]
        if new_id != old_id:
            conn.execute(
                sa.text("UPDATE course_configs SET id = :new WHERE id = :old"),
                {"new": new_id, "old": old_id},
            )

    # Guarantee the column carries AUTO_INCREMENT (no-op where it already does)
    # and reset the table counter to max(id)+1.
    op.execute("ALTER TABLE `course_configs` MODIFY COLUMN `id` INT NOT NULL AUTO_INCREMENT")
    op.execute("ALTER TABLE `course_configs` AUTO_INCREMENT = 1")  # InnoDB clamps up to max(id)+1


def downgrade() -> None:
    # Restore the original id == numeric(course_id) mirror; keep the canonical
    # AUTO_INCREMENT attribute declared by migration 015.
    #
    # The upgrade compacted ids DOWNWARD (e.g. course_id 14 -> id 2), so restoring
    # them moves ids UPWARD, back onto values that are still occupied. A single bulk
    # `UPDATE id = course_id` fails mid-statement with a duplicate-key error: InnoDB
    # rewrites the primary key row-by-row in ascending order and enforces uniqueness
    # immediately, so an early row's target (e.g. 2 -> 14) collides with the row
    # still sitting at 14. Renumber per-row in DESCENDING current-id order instead:
    # each target (course_id >= the row's compacted id) is vacated before it is
    # reused, so no collision is possible. `int(course_id)` fails loudly on a
    # non-numeric course_id rather than silently casting it to 0.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, course_id FROM course_configs ORDER BY id DESC")
    ).fetchall()
    for row in rows:
        old_id, target_id = row[0], int(row[1])
        if target_id != old_id:
            conn.execute(
                sa.text("UPDATE course_configs SET id = :new WHERE id = :old"),
                {"new": target_id, "old": old_id},
            )

    op.execute("ALTER TABLE `course_configs` MODIFY COLUMN `id` INT NOT NULL AUTO_INCREMENT")
    op.execute("ALTER TABLE `course_configs` AUTO_INCREMENT = 1")
