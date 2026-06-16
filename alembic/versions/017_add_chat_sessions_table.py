"""Introduce chat_sessions table; restructure chat_messages; drop chat_session_tags.

Changes:
  1. TRUNCATE chat_messages and DROP chat_session_tags — dev/staging only,
     no real users, existing chat data is disposable.
  2. CREATE chat_sessions — surrogate INT PK, unique session_id string,
     student_id, course_id, tags JSON array, created_at, last_message_at.
  3. ALTER chat_messages — swap VARCHAR session_id + course_id columns for
     a single session_fk INT pointing to chat_sessions.id.

Revision ID: 017
Create Date: 2026-05-13
"""

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Clear old data and drop the tags table ────────────────────────
    op.execute("TRUNCATE TABLE `chat_messages`")
    op.execute("DROP TABLE IF EXISTS `chat_session_tags`")

    # ── 2. Create chat_sessions ──────────────────────────────────────────
    op.execute("""
        CREATE TABLE IF NOT EXISTS `chat_sessions` (
          `id`               INT         NOT NULL AUTO_INCREMENT,
          `session_id`       VARCHAR(50) NOT NULL COMMENT 'Human-readable ID, format: chat_YYYYMMDD_<hex8>',
          `student_id`       INT         NOT NULL COMMENT 'FK to students.id',
          `course_id`        VARCHAR(50) DEFAULT NULL COMMENT 'e.g. SAT, IB-AAHL',
          `tags`             JSON        NOT NULL DEFAULT (JSON_ARRAY()) COMMENT 'Array of {tag, is_preset} objects',
          `created_at`       DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
          `last_message_at`  DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uq_chat_sessions_session_id` (`session_id`),
          KEY `idx_chat_sessions_student` (`student_id`, `last_message_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """)

    # ── 3. Alter chat_messages ───────────────────────────────────────────
    op.execute("ALTER TABLE `chat_messages` DROP INDEX `idx_chat_student_session`")
    op.execute("ALTER TABLE `chat_messages` DROP COLUMN `session_id`")
    op.execute("ALTER TABLE `chat_messages` DROP COLUMN `course_id`")
    op.execute("""
        ALTER TABLE `chat_messages`
        ADD COLUMN `session_fk` INT NOT NULL COMMENT 'FK to chat_sessions.id'
    """)
    op.execute("""
        ALTER TABLE `chat_messages`
        ADD KEY `idx_chat_messages_session_fk` (`session_fk`)
    """)


def downgrade() -> None:
    op.execute("TRUNCATE TABLE `chat_messages`")
    op.execute("ALTER TABLE `chat_messages` DROP INDEX `idx_chat_messages_session_fk`")
    op.execute("ALTER TABLE `chat_messages` DROP COLUMN `session_fk`")
    op.execute("""
        ALTER TABLE `chat_messages`
        ADD COLUMN `session_id` VARCHAR(50) NOT NULL DEFAULT '' COMMENT 'Groups messages into conversations',
        ADD COLUMN `course_id`  VARCHAR(50) DEFAULT NULL COMMENT 'Which course this conversation is about'
    """)
    op.execute("""
        ALTER TABLE `chat_messages`
        ADD KEY `idx_chat_student_session` (`student_id`, `session_id`)
    """)
    op.execute("DROP TABLE IF EXISTS `chat_sessions`")
    op.execute("""
        CREATE TABLE IF NOT EXISTS `chat_session_tags` (
          `id`         BIGINT      NOT NULL AUTO_INCREMENT,
          `student_id` BIGINT      NOT NULL,
          `session_id` VARCHAR(50) NOT NULL,
          `tag`        VARCHAR(64) NOT NULL,
          `is_preset`  TINYINT(1)  NOT NULL DEFAULT 0,
          `created_at` DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (`id`),
          UNIQUE KEY `uq_session_tag` (`session_id`, `tag`),
          KEY `ix_chat_session_tags_student_session` (`student_id`, `session_id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
    """)
