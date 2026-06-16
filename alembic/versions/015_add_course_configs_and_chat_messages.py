"""Add course_configs and chat_messages tables.

Both tables were created manually before alembic was introduced and therefore
were never tracked by a migration. They exist in staging/local but were
missing from production when ``alembic upgrade head`` ran. This migration
backfills the schema so all environments converge.

Uses ``CREATE TABLE IF NOT EXISTS`` so the migration is a no-op where the
tables already exist (staging, local) and creates them where they don't
(production).

DDL is copied verbatim from ``SHOW CREATE TABLE`` on staging to preserve
charset, collation, comments, defaults, and indexes byte-for-byte.

Revision ID: 015
Create Date: 2026-05-12
"""

from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


COURSE_CONFIGS_DDL = """
CREATE TABLE IF NOT EXISTS `course_configs` (
  `id` int NOT NULL AUTO_INCREMENT,
  `course_id` varchar(50) NOT NULL COMMENT 'Unique course identifier, e.g. SAT, IB-AAHL',
  `course_name` varchar(200) NOT NULL COMMENT 'Display name, e.g. SAT Prep - used in system prompt',
  `exam_body` varchar(50) NOT NULL COMMENT 'Exam authority, e.g. College Board, IBO, Edexcel',
  `category` varchar(20) NOT NULL COMMENT 'prep or academic - determines UI behavior',
  `scoring_type` varchar(20) NOT NULL COMMENT 'composite, percentage, or grade - how scores are calculated',
  `max_score` int DEFAULT NULL COMMENT 'Maximum possible score, e.g. 1600 for SAT, 7 for IB',
  `score_components` json DEFAULT NULL COMMENT 'Score breakdown, e.g. {"math": 800, "english": 800}',
  `default_question_types` json DEFAULT NULL COMMENT 'Allowed question types, e.g. ["mcq", "grid_in"]',
  `has_calculator_split` tinyint(1) DEFAULT '0' COMMENT 'Whether exam splits calc/no-calc sections',
  `has_mark_schemes` tinyint(1) DEFAULT '0' COMMENT 'Whether exam uses mark schemes (IB/A-Level)',
  `p_guess_mcq` float DEFAULT '0.25' COMMENT 'Probability of guessing MCQ correctly. SAT = 0.25 (4 options)',
  `p_guess_open` float DEFAULT '0.05' COMMENT 'Probability of guessing open-ended correctly. Grid-in = 0.05',
  `subjects` json NOT NULL COMMENT 'Subject list, e.g. ["math", "english"]',
  `mock_test_label` varchar(50) DEFAULT NULL COMMENT 'Label for mock tests, e.g. Mock SAT',
  `mock_test_duration_minutes` int DEFAULT NULL COMMENT 'Total mock test duration in minutes',
  `mock_test_total_questions` int DEFAULT NULL COMMENT 'Total questions per mock test',
  `pinecone_namespace` varchar(100) DEFAULT NULL COMMENT 'Pinecone namespace for similarity search, e.g. sat-math',
  `extra_taxonomy_fields` json DEFAULT (json_object()) COMMENT 'Future-proofing for course-specific taxonomy fields',
  `is_active` tinyint(1) DEFAULT '1' COMMENT 'Whether this course is currently available',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


CHAT_MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS `chat_messages` (
  `id` int NOT NULL AUTO_INCREMENT,
  `student_id` int NOT NULL COMMENT 'FK to students.id - who sent/received this message',
  `session_id` varchar(50) NOT NULL COMMENT 'Groups messages into conversations, format: chat_YYYYMMDD_NNN',
  `role` varchar(10) NOT NULL COMMENT 'user or assistant',
  `content` text NOT NULL COMMENT 'The actual message text',
  `timestamp` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'When the message was sent',
  `function_calls` json DEFAULT NULL COMMENT 'List of functions the AI called for this response',
  `course_id` varchar(50) DEFAULT NULL COMMENT 'Which course this conversation is about, e.g. SAT',
  PRIMARY KEY (`id`),
  KEY `idx_chat_student_session` (`student_id`,`session_id`),
  KEY `idx_chat_student_time` (`student_id`,`timestamp`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""


def upgrade() -> None:
    op.execute(COURSE_CONFIGS_DDL)
    op.execute(CHAT_MESSAGES_DDL)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS `chat_messages`")
    op.execute("DROP TABLE IF EXISTS `course_configs`")
