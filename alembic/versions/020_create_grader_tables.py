"""Create the AP FRQ auto-grader tables: ap_exam and grading_job.

``ap_exam`` is the exam registry + parse-once rubric cache: the marking scheme
is parsed by Gemini exactly once per exam and the resulting ParsedRubric JSON is
stored here, so every student submission reuses it without re-parsing.

``grading_job`` is the async job + result store: a submission POST inserts a
``queued`` row, an in-process background task grades it and writes the
UI-complete scorecard JSON back, and the client polls for the result.

``CREATE TABLE IF NOT EXISTS`` keeps the migration a no-op where the tables
already exist.

Revision ID: 020
Create Date: 2026-06-02
"""

from alembic import op

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


AP_EXAM_DDL = """
CREATE TABLE IF NOT EXISTS `ap_exam` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `exam_key` varchar(100) NOT NULL COMMENT 'Public exam_id; unique per (course, year, set)',
  `course_id` varchar(50) NOT NULL COMMENT 'FK to course_configs.course_id — drives subject + addenda',
  `year` int NOT NULL,
  `set_label` varchar(50) NOT NULL,
  `is_handwritten` tinyint(1) NOT NULL COMMENT 'true=handwritten (PDF answers), false=typed (app answers)',
  `rubric_json` longtext NOT NULL COMMENT 'Cached ParsedRubric JSON (parse-once; no Gemini on reuse)',
  `questions_pdf_url` varchar(2048) DEFAULT NULL COMMENT 'Durable URL, re-fetched at grade time for handwritten OCR context',
  `marking_scheme_pdf_url` varchar(2048) DEFAULT NULL COMMENT 'Audit / re-parse source',
  `question_map` json DEFAULT NULL COMMENT 'Typed exams: {app_major_question_id: rubric_major_qid}',
  `total_points` float DEFAULT NULL,
  `parse_warnings` json DEFAULT NULL,
  `rubric_parsed_at` datetime DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ap_exam_exam_key` (`exam_key`),
  KEY `idx_ap_exam_course` (`course_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


GRADING_JOB_DDL = """
CREATE TABLE IF NOT EXISTS `grading_job` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `job_key` varchar(64) NOT NULL COMMENT 'Public job_id (UUID)',
  `exam_id` bigint NOT NULL COMMENT 'FK to ap_exam.id',
  `student_id` int NOT NULL,
  `is_handwritten` tinyint(1) NOT NULL COMMENT 'true=handwritten (PDF answers), false=typed (app answers)',
  `answers_pdf_url` varchar(2048) DEFAULT NULL COMMENT 'Handwritten: durable URL to the answer PDF',
  `source_test_id` int DEFAULT NULL COMMENT 'Typed: online_student_test_answers.test_id',
  `source_quiz_id` int DEFAULT NULL COMMENT 'Typed: quiz_student_answer.quiz_id',
  `status` enum('queued','running','succeeded','failed') NOT NULL DEFAULT 'queued',
  `scorecard_json` longtext COMMENT 'Stored GradedScorecardResponse (the poll result)',
  `review_required` tinyint(1) NOT NULL DEFAULT '0',
  `error_message` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `started_at` datetime DEFAULT NULL,
  `finished_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_grading_job_job_key` (`job_key`),
  KEY `idx_grading_job_student_exam` (`student_id`,`exam_id`),
  KEY `idx_grading_job_status` (`status`),
  CONSTRAINT `fk_grading_job_exam` FOREIGN KEY (`exam_id`) REFERENCES `ap_exam` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


def upgrade() -> None:
    op.execute(AP_EXAM_DDL)
    op.execute(GRADING_JOB_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `grading_job`")
    op.execute("DROP TABLE IF EXISTS `ap_exam`")
