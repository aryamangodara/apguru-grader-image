"""Add subtopics table.

One row per LLM-generated subtopic for a given (course, domain, skill).
Subtopics decompose a College Board skill (e.g. "Linear equations in
one variable") into 3-4 finer-grained categories the question
classifier picks from.

Python identifiers (class names, variables, files) use "subtopic"
too; only the LLM-facing prompt files and legacy JSON schemas keep
the "subskill" terminology.

``subtopic_id`` is the stable external identifier (hex) minted by the
generation script so a row keeps its id across re-imports even though
``id`` is assigned by the DB.

Revision ID: 007
Create Date: 2026-04-21
"""

from alembic import op
import sqlalchemy as sa

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subtopics",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("subtopic_id", sa.String(32), nullable=False),
        sa.Column("course_id", sa.Integer, nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("skill", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("identifying_features", sa.JSON, nullable=False),
        sa.Column("example_question", sa.Text, nullable=True),
        sa.Column("solution_approach", sa.Text, nullable=True),
        sa.Column(
            "approximate_frequency",
            sa.Enum("high", "medium", "low", name="subtopic_frequency"),
            nullable=True,
        ),
        sa.Column(
            "difficulty",
            sa.Enum("easy", "medium", "hard", name="subtopic_difficulty"),
            nullable=True,
        ),
        sa.Column(
            "sequence_number",
            sa.Integer,
            nullable=False,
            server_default="1",
        ),
        sa.Column("llm_model", sa.String(100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP,
            nullable=False,
            server_default=sa.text(
                "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
            ),
        ),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "uq_subtopics_external",
        "subtopics",
        ["subtopic_id"],
        unique=True,
    )
    op.create_index(
        "idx_subtopics_skill",
        "subtopics",
        ["course_id", "domain", "skill", "deleted_at"],
    )
    # NOTE: no FK to course(id).  ``course`` is MyISAM, which does not
    # support foreign keys.  The reference is enforced at the ORM layer
    # (app/models/subtopic.py) instead.


def downgrade() -> None:
    op.drop_index("idx_subtopics_skill", table_name="subtopics")
    op.drop_index("uq_subtopics_external", table_name="subtopics")
    op.drop_table("subtopics")
