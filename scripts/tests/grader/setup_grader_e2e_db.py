"""Provision an ISOLATED throwaway database for the grader e2e smoke tests.

Companion to ``test_grader_assessments_e2e.py``. Creates a self-contained DB
(default ``grader_e2e``) **on the MySQL host your .env points at**, holding the
grader schema so the e2e can register/grade/poll without touching any real rows:

  * ``assessment_registry`` + ``grading_job`` — the grader's own tables, created
    from scratch at their current (post-migration-042) shape: ``assessment_registry``
    carries ``assessment_type`` and the composite key ``(assessment_type, test_id)``.
    (Self-contained DDL — no dependency on the source DB already being migrated,
    so this also works against a plain local MySQL.)
  * ``course_configs`` — created lean and **copied from the shared DB** (real
    subjects / exam_body / grading+OCR addenda) so grading resolves authentically.
  * the main-app source tables (``tests`` / ``docs_homework_test`` / ``quiz``)
    are stubbed minimally (only ``id`` + ``deleted_at`` — all
    ``assert_source_is_valid`` reads) and seeded with the ids the e2e registers.

The shared DB is only READ (``course_configs`` copy); nothing in it is written.
Rerunnable — drops + recreates the throwaway DB each run. Teardown when done:
``DROP DATABASE grader_e2e``.

Usage (PowerShell) — set up, point the app at it, run the e2e:

    python scripts/tests/grader/setup_grader_e2e_db.py
    $env:DB_NAME = "grader_e2e"; uvicorn app.main:app --host 127.0.0.1 --port 8080
    # in another shell:
    python scripts/tests/grader/test_grader_assessments_e2e.py

Env:
    DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME  — the shared source DB (.env)
    GRADER_E2E_DB   — throwaway DB name to create (default: grader_e2e)
    GRADER_E2E_HW_ID / _HW_KNOWLEDGE_ID / _QUIZ_ID / _EXAM_ID — seeded source ids
                      (defaults 123 / 124 / 869 / 536; must match the e2e's source ids;
                       _HW_KNOWLEDGE_ID is the rubric-free/no-marking-scheme homework row)

If the schema below ever drifts from the central migrations, re-derive it from
``apguru-centralized-alembic`` (``020`` create + ``026`` test_id identity + ``042``
assessment_type/rename); it is intentionally the minimum the grader SQL touches.
"""
from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path

import aiomysql

_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"

# Grader-owned schema at its post-042 shape. Index/constraint names keep their
# historical ap_exam_* identifiers, matching what the rename migration left in place.
_SCHEMA = [
    """CREATE TABLE `assessment_registry` (
      `id` bigint NOT NULL AUTO_INCREMENT,
      `test_id` bigint NOT NULL,
      `assessment_type` enum('exam','homework','quiz') NOT NULL DEFAULT 'exam',
      `course_id` varchar(50) NOT NULL,
      `test_name` varchar(255) NOT NULL,
      `is_handwritten` tinyint(1) NOT NULL,
      `rubric_json` longtext NOT NULL,
      `questions_pdf_url` varchar(2048) DEFAULT NULL,
      `marking_scheme_pdf_url` varchar(2048) DEFAULT NULL,
      `total_points` float DEFAULT NULL,
      `parse_warnings` json DEFAULT NULL,
      `rubric_parsed_at` datetime DEFAULT NULL,
      `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
      `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      `deleted_at` datetime DEFAULT NULL,
      PRIMARY KEY (`id`),
      UNIQUE KEY `uq_ap_exam_type_test_id` (`assessment_type`, `test_id`),
      KEY `idx_ap_exam_course` (`course_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE `grading_job` (
      `id` bigint NOT NULL AUTO_INCREMENT,
      `job_key` varchar(64) NOT NULL,
      `exam_id` bigint NOT NULL,
      `student_id` int NOT NULL,
      `is_handwritten` tinyint(1) NOT NULL,
      `answers_pdf_url` varchar(2048) DEFAULT NULL,
      `answers_json` json DEFAULT NULL,
      `status` enum('queued','running','succeeded','failed') NOT NULL DEFAULT 'queued',
      `scorecard_json` longtext,
      `review_required` tinyint(1) NOT NULL DEFAULT '0',
      `error_message` text,
      `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
      `started_at` datetime DEFAULT NULL,
      `finished_at` datetime DEFAULT NULL,
      PRIMARY KEY (`id`),
      UNIQUE KEY `uq_grading_job_job_key` (`job_key`),
      KEY `idx_grading_job_student_exam` (`student_id`, `exam_id`),
      KEY `idx_grading_job_status` (`status`),
      CONSTRAINT `fk_grading_job_exam` FOREIGN KEY (`exam_id`) REFERENCES `assessment_registry` (`id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
    """CREATE TABLE `course_configs` (
      `id` int NOT NULL AUTO_INCREMENT,
      `course_id` varchar(50) NOT NULL,
      `course_name` varchar(200) NOT NULL,
      `exam_body` varchar(50) NOT NULL,
      `category` varchar(20) NOT NULL DEFAULT 'prep',
      `scoring_type` varchar(20) NOT NULL DEFAULT 'percentage',
      `max_score` int DEFAULT NULL,
      `subjects` json NOT NULL,
      `grading_addendum` text,
      `ocr_addendum` text,
      `is_active` tinyint(1) DEFAULT '1',
      `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (`id`),
      UNIQUE KEY `uq_course_configs_course_id` (`course_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci""",
]
_SOURCE_TABLE_DDL = (
    "CREATE TABLE `{t}` (`id` bigint NOT NULL, `deleted_at` datetime DEFAULT NULL, "
    "PRIMARY KEY (`id`)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"
)
_COURSE_COLS = (
    "course_id, course_name, exam_body, category, scoring_type, max_score, subjects, "
    "grading_addendum, ocr_addendum, is_active"
)


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        m = re.match(r"([A-Z_]+)=(.*)", line)
        if m:
            env[m.group(1)] = m.group(2).strip().strip('"')
    return env


async def main() -> None:
    env = _load_env()
    shared = env["DB_NAME"]  # read-only source for the course_configs copy
    e2e = os.environ.get("GRADER_E2E_DB", "grader_e2e")
    hw_id = int(os.environ.get("GRADER_E2E_HW_ID", "123"))
    # Second homework row for the rubric-free (no marking scheme) knowledge-grading case.
    hw_knowledge_id = int(os.environ.get("GRADER_E2E_HW_KNOWLEDGE_ID", "124"))
    quiz_id = int(os.environ.get("GRADER_E2E_QUIZ_ID", "869"))
    exam_id = int(os.environ.get("GRADER_E2E_EXAM_ID", "536"))

    conn = await aiomysql.connect(
        host=env["DB_HOST"], port=int(env.get("DB_PORT", 3306)),
        user=env["DB_USER"], password=env["DB_PASSWORD"], autocommit=True,
    )
    cur = await conn.cursor()

    await cur.execute(f"DROP DATABASE IF EXISTS `{e2e}`")
    await cur.execute(f"CREATE DATABASE `{e2e}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci")
    await cur.execute(f"USE `{e2e}`")
    for stmt in _SCHEMA:
        await cur.execute(stmt)
    for table in ("tests", "docs_homework_test", "quiz"):
        await cur.execute(_SOURCE_TABLE_DDL.format(t=table))

    # real course config (subjects / exam_body / addenda) from the shared DB
    await cur.execute(
        f"INSERT INTO `{e2e}`.course_configs ({_COURSE_COLS}) "
        f"SELECT {_COURSE_COLS} FROM `{shared}`.course_configs WHERE is_active = 1"
    )
    await cur.execute(f"INSERT INTO `{e2e}`.tests (id) VALUES ({exam_id})")
    await cur.execute(f"INSERT INTO `{e2e}`.docs_homework_test (id) VALUES ({hw_id}), ({hw_knowledge_id})")
    await cur.execute(f"INSERT INTO `{e2e}`.quiz (id) VALUES ({quiz_id})")

    await cur.execute(f"SELECT COUNT(*) FROM `{e2e}`.course_configs")
    (n_courses,) = await cur.fetchone()
    conn.close()

    print(f"OK: created `{e2e}` on {env['DB_HOST']}")
    print(f"  course_configs copied: {n_courses} rows")
    print(
        f"  seeded source ids -> tests={exam_id}, "
        f"docs_homework_test={hw_id},{hw_knowledge_id}, quiz={quiz_id}"
    )
    print(f"  next: set DB_NAME={e2e}, run the app, then test_grader_assessments_e2e.py")


if __name__ == "__main__":
    asyncio.run(main())
