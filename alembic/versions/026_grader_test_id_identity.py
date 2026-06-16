"""Re-key the AP FRQ grader off tests.id; inline typed answers.

Replaces ``ap_exam``'s synthetic ``exam_key`` (course/year/set_label) with the
``test_id`` it grades — the identifier every grader API now works on — and adds a
free-text ``test_name`` for the scorecard label. ``grading_job`` swaps its
DB-pull ``source_test_id`` / ``source_quiz_id`` for an inline ``answers_json``
payload (typed exams now submit ``{major_qid: answer_text}`` directly).

Existing grader rows are keyed by the old scheme and cannot be migrated in place,
so they are cleared — re-register exams afterward (the grader is parse-once and
re-registration is cheap; see scripts/tests/grader/register_all_exams.py).

Revision ID: 026
Create Date: 2026-06-15
"""

from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Old grader data is keyed by exam_key and is disposable; clear children
    # first to satisfy the grading_job -> ap_exam foreign key.
    op.execute("DELETE FROM grading_job")
    op.execute("DELETE FROM ap_exam")

    op.execute(
        "ALTER TABLE `ap_exam` "
        "ADD COLUMN `test_id` bigint NOT NULL "
        "COMMENT 'tests.id this exam grades — public identifier' AFTER `id`, "
        "ADD COLUMN `test_name` varchar(255) NOT NULL "
        "COMMENT 'Human label shown on the scorecard (replaces set_label)' AFTER `course_id`, "
        "DROP COLUMN `exam_key`, "
        "DROP COLUMN `year`, "
        "DROP COLUMN `set_label`, "
        "DROP COLUMN `question_map`, "
        "ADD UNIQUE KEY `uq_ap_exam_test_id` (`test_id`)"
    )

    op.execute(
        "ALTER TABLE `grading_job` "
        "ADD COLUMN `answers_json` json DEFAULT NULL "
        "COMMENT 'Typed: inline {major_qid: answer_text} submitted for grading' AFTER `answers_pdf_url`, "
        "DROP COLUMN `source_test_id`, "
        "DROP COLUMN `source_quiz_id`"
    )


def downgrade() -> None:
    # Reverse the schema; data is not restored (upgrade cleared it).
    op.execute("DELETE FROM grading_job")
    op.execute("DELETE FROM ap_exam")

    op.execute(
        "ALTER TABLE `ap_exam` "
        "ADD COLUMN `exam_key` varchar(100) NOT NULL "
        "COMMENT 'Public exam_id; unique per (course, year, set)' AFTER `id`, "
        "ADD COLUMN `year` int NOT NULL AFTER `course_id`, "
        "ADD COLUMN `set_label` varchar(50) NOT NULL AFTER `year`, "
        "ADD COLUMN `question_map` json DEFAULT NULL AFTER `marking_scheme_pdf_url`, "
        "DROP COLUMN `test_id`, "
        "DROP COLUMN `test_name`, "
        "ADD UNIQUE KEY `uq_ap_exam_exam_key` (`exam_key`)"
    )

    op.execute(
        "ALTER TABLE `grading_job` "
        "ADD COLUMN `source_test_id` int DEFAULT NULL "
        "COMMENT 'Typed: online_student_test_answers.test_id' AFTER `answers_pdf_url`, "
        "ADD COLUMN `source_quiz_id` int DEFAULT NULL "
        "COMMENT 'Typed: quiz_student_answer.quiz_id' AFTER `source_test_id`, "
        "DROP COLUMN `answers_json`"
    )
