"""Cached accessor for the course_configs table.

Single source of truth for all course-specific logic.

WHO CALLS THIS:
    build_tutor_system_prompt()  → course_name, max_score, score_components
    SM-2 spaced repetition       → p_guess_mcq, p_guess_open
    find_similar_questions()     → pinecone_namespace
    Score calculations           → scoring_type, max_score, score_components

CACHING:
    In-memory dict cache — permanent until server restart or manual clear.
    Course configs never change mid-session.
"""

from typing import Any

from app.core.database import Database
from app.core.errors import UnknownCourseError

# ─────────────────────────────────────────────
# Async-compatible in-memory cache
# ─────────────────────────────────────────────
_course_config_cache: dict[int, dict[str, Any]] = {}


async def get_course_config(course_id: int) -> dict[str, Any]:
    """Fetches full course config row as a dict. Cached permanently.

    Usage:
        config = await get_course_config(1)  # 1 = SAT
        config["max_score"]                  # 1600
        config["score_components"]           # {"math": 800, "english": 800}
    """
    if course_id in _course_config_cache:
        return _course_config_cache[course_id]

    db = Database.get_instance()
    row = await db.query_one(
        "SELECT * FROM course_configs WHERE course_id = :course_id AND is_active = 1",
        {"course_id": course_id},
    )
    if not row:
        raise UnknownCourseError(f"Unknown course_id: {course_id}")

    _course_config_cache[course_id] = row
    return row


async def get_course_name(course_id: int) -> str:
    config = await get_course_config(course_id)
    return config.get("course_name", "")


async def get_max_score(course_id: int) -> int:
    config = await get_course_config(course_id)
    return config.get("max_score", 0)


async def get_score_components(course_id: int) -> dict[str, Any]:
    config = await get_course_config(course_id)
    return config.get("score_components", {})


async def get_p_guess(course_id: int, question_type: str) -> float:
    config = await get_course_config(course_id)
    if question_type == "mcq":
        return config.get("p_guess_mcq", 0.25)
    return config.get("p_guess_open", 0.05)


async def get_pinecone_namespace(course_id: int) -> str:
    config = await get_course_config(course_id)
    return config.get("pinecone_namespace", "")


async def get_grading_addendum(course_id: int) -> str:
    """Subject-specific grading guidance for the AP FRQ grader (may be empty).

    Stored in ``course_configs.grading_addendum`` and injected verbatim into the
    grading prompt at grade time, so guidance is editable without a deploy. Call
    ``clear_course_config_cache()`` after editing it so the next read re-caches.
    """
    config = await get_course_config(course_id)
    return config.get("grading_addendum") or ""


async def get_ocr_addendum(course_id: int) -> str:
    """Subject-specific OCR/diagram guidance for the AP FRQ grader (may be empty).

    Stored in ``course_configs.ocr_addendum`` and injected into the OCR prompt
    for handwritten exams. Same cache-clear note as ``get_grading_addendum``.
    """
    config = await get_course_config(course_id)
    return config.get("ocr_addendum") or ""


def clear_course_config_cache() -> None:
    """Call if you update course_configs via admin panel."""
    _course_config_cache.clear()
